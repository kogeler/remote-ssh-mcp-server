"""Bounded MCP STDIO interface for the Remote SSH server."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import BaseModel, ValidationError

from . import __version__
from .commands import CommandRunner
from .config import RuntimeConfig
from .errors import RemoteMCPError
from .inspection import RemoteInspector
from .local_paths import LocalPathPolicy
from .master import OpenSSHMaster
from .mcp_models import (
    CommandData,
    CommandResponse,
    ConnectInput,
    ConnectionData,
    ConnectionResponse,
    DirectoryData,
    DirectoryResponse,
    DownloadStartInput,
    EmptyInput,
    ExecInput,
    ListDirectoryInput,
    PublicError,
    ReadFileRangeData,
    ReadFileRangeInput,
    ReadFileRangeResponse,
    StatData,
    StatInput,
    StatResponse,
    TransferData,
    TransferIdInput,
    TransferListResponse,
    TransferResponse,
    UploadStartInput,
)
from .sudo import SudoRunner
from .transfers import TransferManager

SERVER_INSTRUCTIONS = (
    "This server starts disconnected. Call connect deliberately with either one "
    "ssh_alias or one host/user pair with an optional port; the call may open the "
    "normal system SSH "
    "authentication UI. At most one authenticated OpenSSH master exists at a time, "
    "and changing targets requires disconnect first. Never request or pass a password, "
    "PIN, private key, sudo secret, SSH option, or absolute local path. There is no "
    "automatic reconnect after connection_lost; only another explicit connect may "
    "authenticate. exec, sudo_exec, uploads, downloads, cancellation, disconnect, and "
    "overwrite operations can change state and require deliberate approval. Local paths "
    "are relative to the server's local root. Commands are isolated non-PTY shells; cwd and "
    "environment changes do not persist. Output is bounded and may be truncated or "
    "explicitly spooled. Large files use background rsync: start a transfer, poll "
    "transfer_status, and cancel only when required. sudo_exec succeeds only for "
    "NOPASSWD policy because it always uses sudo -n -k."
)


READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
CONNECTING = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
MUTATING = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
CANCELLING = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)


class RemoteSSHApplication:
    """Own one explicit SSH connection at a time for an MCP server lifespan."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.paths = LocalPathPolicy(config.repository_root)
        self.master: OpenSSHMaster | None = None
        self.runner: CommandRunner | None = None
        self.inspector: RemoteInspector | None = None
        self.sudo: SudoRunner | None = None
        self.transfers: TransferManager | None = None
        self._connection_lock = asyncio.Lock()

    async def start(self) -> None:
        self.paths.initialize()

    @staticmethod
    def disconnected_status() -> dict[str, str | int | None]:
        return {
            "state": "disconnected",
            "mode": None,
            "target": None,
            "ssh_alias": None,
            "host": None,
            "user": None,
            "port": None,
            "master_pid": None,
        }

    async def connect(self, request: ConnectInput) -> dict[str, str | int | None]:
        async with self._connection_lock:
            if self.master is not None:
                code = (
                    "already_connected"
                    if self.master.state.value in {"starting", "ready"}
                    else "disconnect_required"
                )
                raise RemoteMCPError(
                    code,
                    "disconnect the current SSH lifecycle before connecting again",
                )

            master = OpenSSHMaster(self.config, request.connection_spec())
            self.master = master
            try:
                await master.start()
                runner = CommandRunner(self.config, master, self.paths)
                inspector = RemoteInspector(runner)
                self.runner = runner
                self.inspector = inspector
                self.sudo = SudoRunner(runner)
                self.transfers = TransferManager(
                    self.config,
                    master,
                    self.paths,
                    runner,
                    inspector,
                )
                return master.status()
            except BaseException:
                await master.close()
                self.master = None
                raise

    async def connection_status(self) -> dict[str, str | int | None]:
        master = self.master
        if master is None:
            return self.disconnected_status()
        if master.state.value == "ready":
            try:
                await master.ensure_ready()
            except RemoteMCPError:
                pass
        return master.status()

    async def disconnect(self) -> dict[str, str | int | None]:
        async with self._connection_lock:
            transfers = self.transfers
            runner = self.runner
            master = self.master
            self.runner = None
            self.inspector = None
            self.sudo = None
            self.transfers = None
            self.master = None
            try:
                if transfers is not None:
                    await transfers.close()
            finally:
                try:
                    if runner is not None:
                        await runner.close()
                finally:
                    if master is not None:
                        await master.close()
            return self.disconnected_status()

    async def close(self) -> None:
        await self.disconnect()

    def require_services(
        self,
    ) -> tuple[CommandRunner, RemoteInspector, SudoRunner, TransferManager]:
        if None in (
            self.master,
            self.runner,
            self.inspector,
            self.sudo,
            self.transfers,
        ):
            raise RemoteMCPError(
                "not_connected", "call connect before using remote operation tools"
            )
        assert self.runner is not None
        assert self.inspector is not None
        assert self.sudo is not None
        assert self.transfers is not None
        return self.runner, self.inspector, self.sudo, self.transfers

    def public_transfer(self, value: dict[str, Any]) -> TransferData:
        sanitized = dict(value)
        replacements = [(str(self.config.repository_root), "<repository>")]
        if self.master is not None and self.master.runtime_dir is not None:
            replacements.append((str(self.master.runtime_dir), "<runtime-dir>"))
        if self.master is not None and self.master.control_path is not None:
            replacements.append((str(self.master.control_path), "<control-path>"))
        for field in ("stdout_tail", "stderr_tail"):
            text = str(sanitized[field])
            for sensitive, replacement in replacements:
                text = text.replace(sensitive, replacement)
            sanitized[field] = text
        internal_error = sanitized.get("error")
        if internal_error is not None:
            sanitized["error"] = {
                "code": internal_error.get("error", "transfer_failed"),
                "message": internal_error.get("message", "transfer failed"),
            }
        return TransferData.model_validate(sanitized)


Handler = Callable[[RemoteSSHApplication, BaseModel], Awaitable[BaseModel]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    annotations: ToolAnnotations
    handler: Handler

    def protocol_tool(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            annotations=self.annotations,
        )


def _error_response(
    response_model: type[BaseModel], code: str, message: str
) -> BaseModel:
    return response_model(
        ok=False,
        error=PublicError(code=code, message=message),
    )


async def _connection_status(
    app: RemoteSSHApplication, _request: BaseModel
) -> ConnectionResponse:
    return ConnectionResponse(
        ok=True,
        result=ConnectionData.model_validate(await app.connection_status()),
    )


async def _connect(app: RemoteSSHApplication, request: BaseModel) -> ConnectionResponse:
    assert isinstance(request, ConnectInput)
    return ConnectionResponse(
        ok=True,
        result=ConnectionData.model_validate(await app.connect(request)),
    )


async def _disconnect(
    app: RemoteSSHApplication, _request: BaseModel
) -> ConnectionResponse:
    return ConnectionResponse(
        ok=True,
        result=ConnectionData.model_validate(await app.disconnect()),
    )


async def _exec(app: RemoteSSHApplication, request: BaseModel) -> CommandResponse:
    assert isinstance(request, ExecInput)
    runner, _inspector, _sudo, _transfers = app.require_services()
    result = await runner.execute(
        request.command,
        cwd=request.cwd,
        timeout=request.timeout,
        spool_output=request.spool_output,
    )
    return CommandResponse(ok=True, result=CommandData.model_validate(result.to_dict()))


async def _sudo_exec(app: RemoteSSHApplication, request: BaseModel) -> CommandResponse:
    assert isinstance(request, ExecInput)
    _runner, _inspector, sudo, _transfers = app.require_services()
    result = await sudo.execute(
        request.command,
        cwd=request.cwd,
        timeout=request.timeout,
        spool_output=request.spool_output,
    )
    return CommandResponse(ok=True, result=CommandData.model_validate(result.to_dict()))


async def _stat(app: RemoteSSHApplication, request: BaseModel) -> StatResponse:
    assert isinstance(request, StatInput)
    _runner, inspector, _sudo, _transfers = app.require_services()
    return StatResponse(
        ok=True,
        result=StatData.model_validate(await inspector.stat(request.remote_path)),
    )


async def _list_directory(
    app: RemoteSSHApplication, request: BaseModel
) -> DirectoryResponse:
    assert isinstance(request, ListDirectoryInput)
    _runner, inspector, _sudo, _transfers = app.require_services()
    return DirectoryResponse(
        ok=True,
        result=DirectoryData.model_validate(
            await inspector.list_directory(request.remote_path)
        ),
    )


async def _read_file_range(
    app: RemoteSSHApplication, request: BaseModel
) -> ReadFileRangeResponse:
    assert isinstance(request, ReadFileRangeInput)
    _runner, inspector, _sudo, _transfers = app.require_services()
    max_bytes = request.max_bytes
    if "max_bytes" not in request.model_fields_set:
        max_bytes = min(max_bytes, app.config.max_output_bytes)
    value = await inspector.read_file_range(
        request.remote_path,
        offset=request.offset,
        max_bytes=max_bytes,
    )
    return ReadFileRangeResponse(
        ok=True, result=ReadFileRangeData.model_validate(value)
    )


async def _download_start(
    app: RemoteSSHApplication, request: BaseModel
) -> TransferResponse:
    assert isinstance(request, DownloadStartInput)
    _runner, _inspector, _sudo, transfers = app.require_services()
    value = await transfers.start_download(
        request.remote_path,
        request.local_path,
        overwrite=request.overwrite,
    )
    return TransferResponse(ok=True, result=app.public_transfer(value))


async def _upload_start(
    app: RemoteSSHApplication, request: BaseModel
) -> TransferResponse:
    assert isinstance(request, UploadStartInput)
    _runner, _inspector, _sudo, transfers = app.require_services()
    value = await transfers.start_upload(
        request.local_path,
        request.remote_path,
        overwrite=request.overwrite,
    )
    return TransferResponse(ok=True, result=app.public_transfer(value))


async def _transfer_status(
    app: RemoteSSHApplication, request: BaseModel
) -> TransferResponse:
    assert isinstance(request, TransferIdInput)
    _runner, _inspector, _sudo, transfers = app.require_services()
    value = await transfers.status(request.operation_id)
    return TransferResponse(ok=True, result=app.public_transfer(value))


async def _transfer_cancel(
    app: RemoteSSHApplication, request: BaseModel
) -> TransferResponse:
    assert isinstance(request, TransferIdInput)
    _runner, _inspector, _sudo, transfers = app.require_services()
    value = await transfers.cancel(request.operation_id)
    return TransferResponse(ok=True, result=app.public_transfer(value))


async def _transfer_list(
    app: RemoteSSHApplication, _request: BaseModel
) -> TransferListResponse:
    _runner, _inspector, _sudo, transfers = app.require_services()
    values = await transfers.list()
    return TransferListResponse(
        ok=True,
        result=[app.public_transfer(value) for value in values],
    )


TOOL_DEFINITIONS = (
    ToolDefinition(
        "connect",
        "Open one SSH master using an alias or host/user with an optional port.",
        ConnectInput,
        ConnectionResponse,
        CONNECTING,
        _connect,
    ),
    ToolDefinition(
        "disconnect",
        "Cancel active commands and transfers, then close the owned SSH master.",
        EmptyInput,
        ConnectionResponse,
        CANCELLING,
        _disconnect,
    ),
    ToolDefinition(
        "connection_status",
        "Report disconnected, starting, ready, or lost without opening a connection.",
        EmptyInput,
        ConnectionResponse,
        READ_ONLY,
        _connection_status,
    ),
    ToolDefinition(
        "exec",
        "Run one bounded non-PTY command on the connected target.",
        ExecInput,
        CommandResponse,
        MUTATING,
        _exec,
    ),
    ToolDefinition(
        "sudo_exec",
        "Run one command through passwordless-only sudo -n -k.",
        ExecInput,
        CommandResponse,
        MUTATING,
        _sudo_exec,
    ),
    ToolDefinition(
        "stat",
        "Inspect metadata for one remote path.",
        StatInput,
        StatResponse,
        READ_ONLY,
        _stat,
    ),
    ToolDefinition(
        "list_directory",
        "List one remote directory with bounded machine-readable metadata.",
        ListDirectoryInput,
        DirectoryResponse,
        READ_ONLY,
        _list_directory,
    ),
    ToolDefinition(
        "read_file_range",
        "Read a bounded byte range from one remote regular file.",
        ReadFileRangeInput,
        ReadFileRangeResponse,
        READ_ONLY,
        _read_file_range,
    ),
    ToolDefinition(
        "download_start",
        "Start a verified background rsync download into the server's local root.",
        DownloadStartInput,
        TransferResponse,
        MUTATING,
        _download_start,
    ),
    ToolDefinition(
        "upload_start",
        "Start a verified background rsync upload without sudo.",
        UploadStartInput,
        TransferResponse,
        MUTATING,
        _upload_start,
    ),
    ToolDefinition(
        "transfer_status",
        "Return metadata for one background transfer.",
        TransferIdInput,
        TransferResponse,
        READ_ONLY,
        _transfer_status,
    ),
    ToolDefinition(
        "transfer_cancel",
        "Cancel one transfer while preserving its resumable partial.",
        TransferIdInput,
        TransferResponse,
        CANCELLING,
        _transfer_cancel,
    ),
    ToolDefinition(
        "transfer_list",
        "List retained background transfer metadata.",
        EmptyInput,
        TransferListResponse,
        READ_ONLY,
        _transfer_list,
    ),
)
TOOLS_BY_NAME = {definition.name: definition for definition in TOOL_DEFINITIONS}


def _tool_result(payload: BaseModel, *, is_error: bool) -> CallToolResult:
    structured = payload.model_dump(mode="json")
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    structured,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        ],
        structured_content=structured,
        is_error=is_error,
    )


def create_mcp_server(config: RuntimeConfig) -> Server[RemoteSSHApplication]:
    app = RemoteSSHApplication(config)

    @asynccontextmanager
    async def lifespan(_server: Server[Any]) -> AsyncIterator[RemoteSSHApplication]:
        await app.start()
        try:
            yield app
        finally:
            await app.close()

    async def list_tools(
        _ctx: ServerRequestContext[RemoteSSHApplication],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(
            tools=[definition.protocol_tool() for definition in TOOL_DEFINITIONS]
        )

    async def call_tool(
        ctx: ServerRequestContext[RemoteSSHApplication],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        definition = TOOLS_BY_NAME.get(params.name)
        if definition is None:
            payload = _error_response(
                TransferResponse, "unknown_tool", "requested tool is not available"
            )
            return _tool_result(payload, is_error=True)
        try:
            request = definition.input_model.model_validate(params.arguments or {})
        except ValidationError:
            payload = _error_response(
                definition.output_model,
                "invalid_arguments",
                "tool arguments failed strict validation",
            )
            return _tool_result(payload, is_error=True)
        try:
            payload = await definition.handler(ctx.lifespan_context, request)
        except RemoteMCPError as error:
            payload = _error_response(
                definition.output_model, error.code, error.message
            )
            return _tool_result(payload, is_error=True)
        except Exception as error:  # noqa: BLE001 - MCP must return a safe error.
            logging.getLogger(__name__).error(
                "tool %s failed internally (%s)",
                definition.name,
                type(error).__name__,
            )
            payload = _error_response(
                definition.output_model,
                "internal_error",
                "tool failed internally",
            )
            return _tool_result(payload, is_error=True)
        return _tool_result(payload, is_error=False)

    return Server(
        "remote-ssh-mcp",
        title="Remote SSH MCP",
        description="Explicitly connected, bounded operations over one SSH master.",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_stdio(config: RuntimeConfig) -> None:
    server = create_mcp_server(config)
    current_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    shutdown_requested = False

    def request_shutdown() -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        if current_task is not None and not current_task.done():
            current_task.cancel()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_shutdown)
            installed_signals.append(signum)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    except asyncio.CancelledError:
        if not shutdown_requested:
            raise
        logging.getLogger(__name__).info("shutdown requested")
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
