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
from .config import RuntimeConfig
from .errors import RemoteMCPError
from .local_paths import LocalPathPolicy
from .mcp_models import (
    CommandData,
    CommandResponse,
    ConnectData,
    ConnectInput,
    ConnectResponse,
    DirectoryData,
    DirectoryResponse,
    DisconnectInput,
    DownloadStartInput,
    EmptyInput,
    ExecInput,
    ListDirectoryInput,
    PublicError,
    ReadFileRangeData,
    ReadFileRangeInput,
    ReadFileRangeResponse,
    SessionData,
    SessionInput,
    SessionListResponse,
    SessionResponse,
    StatData,
    StatInput,
    StatResponse,
    TransferData,
    TransferIdInput,
    TransferListResponse,
    TransferResponse,
    UploadStartInput,
)
from .sessions import RemoteSession, SessionManager, SessionServices

SERVER_INSTRUCTIONS = (
    "This server starts with no SSH sessions. Call connect deliberately with either "
    "one ssh_alias or one host/user pair with an optional port; the call may open "
    "the normal system SSH authentication UI. Never request or pass a password, PIN, "
    "private key, sudo secret, SSH option, or absolute local path. Each connect "
    "opens an independent session with its own OpenSSH master and returns a public "
    "session_id plus a secret session_key exactly once; no tool returns that key "
    "again. Pass both values to every remote operation, keep the key in your own "
    "context, and never reveal it to another agent unless deliberately delegating "
    "that session. At most one session may own a remote server, identified by its "
    "effective OpenSSH HostName and Port; another connect to it fails with "
    "already_connected. session_list shows every session without keys to every "
    "caller. disconnect needs only session_id and can close any session, so close "
    "only your own sessions or sessions the user asked you to close; if you lose a "
    "key, disconnect that session and connect again. There is no automatic "
    "reconnect after connection_lost; only another explicit connect may "
    "authenticate. exec, sudo_exec, uploads, downloads, cancellation, disconnect, "
    "and overwrite operations can change state and require deliberate approval. "
    "Local paths are relative to the server's local root shared by all sessions. "
    "Commands are isolated non-PTY shells; cwd and environment changes do not "
    "persist. Output is bounded and may be truncated or explicitly spooled. Large "
    "files use background rsync: start a transfer, poll transfer_status, and cancel "
    "only when required. sudo_exec succeeds only for NOPASSWD policy because it "
    "always uses sudo -n -k."
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
    """Adapt independent keyed SSH sessions to one MCP server lifespan."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.paths = LocalPathPolicy(config.repository_root)
        self.sessions = SessionManager(config, self.paths)

    async def start(self) -> None:
        self.paths.initialize()

    async def close(self) -> None:
        await self.sessions.close()

    def require_session(
        self, request: SessionInput
    ) -> tuple[RemoteSession, SessionServices]:
        return self.sessions.authorize(request.session_id, request.session_key)

    def public_transfer(
        self, session: RemoteSession, value: dict[str, Any]
    ) -> TransferData:
        sanitized = dict(value)
        master = session.master
        replacements = [(str(self.config.repository_root), "<repository>")]
        if master.runtime_dir is not None:
            replacements.append((str(master.runtime_dir), "<runtime-dir>"))
        if master.control_path is not None:
            replacements.append((str(master.control_path), "<control-path>"))
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


def _validation_message(model: type[BaseModel], error: ValidationError) -> str:
    """Name only schema fields; never echo argument values or unknown names."""
    details = error.errors(
        include_url=False, include_context=False, include_input=False
    )
    fields = sorted(
        {
            str(detail["loc"][0])
            for detail in details
            if detail["loc"]
            and detail["type"] != "extra_forbidden"
            and detail["loc"][0] in model.model_fields
        }
    )
    message = "tool arguments failed strict validation"
    if fields:
        message += f"; missing or invalid fields: {', '.join(fields)}"
    if any(detail["type"] == "extra_forbidden" for detail in details):
        message += "; unknown fields are not allowed"
    if {"session_id", "session_key"} & set(fields):
        message += "; pass the session_id and session_key returned by connect"
    return message


def _error_response(
    response_model: type[BaseModel], code: str, message: str
) -> BaseModel:
    return response_model(
        ok=False,
        error=PublicError(code=code, message=message),
    )


async def _session_list(
    app: RemoteSSHApplication, _request: BaseModel
) -> SessionListResponse:
    return SessionListResponse(
        ok=True,
        result=[
            SessionData.model_validate(value)
            for value in await app.sessions.list_sessions()
        ],
    )


async def _connect(app: RemoteSSHApplication, request: BaseModel) -> ConnectResponse:
    assert isinstance(request, ConnectInput)
    session, key = await app.sessions.connect(request.connection_spec())
    return ConnectResponse(
        ok=True,
        result=ConnectData.model_validate({**session.status(), "session_key": key}),
    )


async def _disconnect(app: RemoteSSHApplication, request: BaseModel) -> SessionResponse:
    assert isinstance(request, DisconnectInput)
    return SessionResponse(
        ok=True,
        result=SessionData.model_validate(
            await app.sessions.disconnect(request.session_id)
        ),
    )


async def _exec(app: RemoteSSHApplication, request: BaseModel) -> CommandResponse:
    assert isinstance(request, ExecInput)
    _session, services = app.require_session(request)
    result = await services.runner.execute(
        request.command,
        cwd=request.cwd,
        timeout=request.timeout,
        spool_output=request.spool_output,
    )
    return CommandResponse(ok=True, result=CommandData.model_validate(result.to_dict()))


async def _sudo_exec(app: RemoteSSHApplication, request: BaseModel) -> CommandResponse:
    assert isinstance(request, ExecInput)
    _session, services = app.require_session(request)
    result = await services.sudo.execute(
        request.command,
        cwd=request.cwd,
        timeout=request.timeout,
        spool_output=request.spool_output,
    )
    return CommandResponse(ok=True, result=CommandData.model_validate(result.to_dict()))


async def _stat(app: RemoteSSHApplication, request: BaseModel) -> StatResponse:
    assert isinstance(request, StatInput)
    _session, services = app.require_session(request)
    return StatResponse(
        ok=True,
        result=StatData.model_validate(
            await services.inspector.stat(request.remote_path)
        ),
    )


async def _list_directory(
    app: RemoteSSHApplication, request: BaseModel
) -> DirectoryResponse:
    assert isinstance(request, ListDirectoryInput)
    _session, services = app.require_session(request)
    return DirectoryResponse(
        ok=True,
        result=DirectoryData.model_validate(
            await services.inspector.list_directory(request.remote_path)
        ),
    )


async def _read_file_range(
    app: RemoteSSHApplication, request: BaseModel
) -> ReadFileRangeResponse:
    assert isinstance(request, ReadFileRangeInput)
    _session, services = app.require_session(request)
    max_bytes = request.max_bytes
    if "max_bytes" not in request.model_fields_set:
        max_bytes = min(max_bytes, app.config.max_output_bytes)
    value = await services.inspector.read_file_range(
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
    session, services = app.require_session(request)
    value = await services.transfers.start_download(
        request.remote_path,
        request.local_path,
        overwrite=request.overwrite,
    )
    return TransferResponse(ok=True, result=app.public_transfer(session, value))


async def _upload_start(
    app: RemoteSSHApplication, request: BaseModel
) -> TransferResponse:
    assert isinstance(request, UploadStartInput)
    session, services = app.require_session(request)
    value = await services.transfers.start_upload(
        request.local_path,
        request.remote_path,
        overwrite=request.overwrite,
    )
    return TransferResponse(ok=True, result=app.public_transfer(session, value))


async def _transfer_status(
    app: RemoteSSHApplication, request: BaseModel
) -> TransferResponse:
    assert isinstance(request, TransferIdInput)
    session, services = app.require_session(request)
    value = await services.transfers.status(request.operation_id)
    return TransferResponse(ok=True, result=app.public_transfer(session, value))


async def _transfer_cancel(
    app: RemoteSSHApplication, request: BaseModel
) -> TransferResponse:
    assert isinstance(request, TransferIdInput)
    session, services = app.require_session(request)
    value = await services.transfers.cancel(request.operation_id)
    return TransferResponse(ok=True, result=app.public_transfer(session, value))


async def _transfer_list(
    app: RemoteSSHApplication, request: BaseModel
) -> TransferListResponse:
    assert isinstance(request, SessionInput)
    session, services = app.require_session(request)
    values = await services.transfers.list()
    return TransferListResponse(
        ok=True,
        result=[app.public_transfer(session, value) for value in values],
    )


TOOL_DEFINITIONS = (
    ToolDefinition(
        "connect",
        "Open a new independent SSH session using an alias or host/user with an "
        "optional port; returns its session_id and a one-time secret session_key.",
        ConnectInput,
        ConnectResponse,
        CONNECTING,
        _connect,
    ),
    ToolDefinition(
        "disconnect",
        "Close any SSH session by session_id without its key, cancelling its "
        "active commands and transfers.",
        DisconnectInput,
        SessionResponse,
        CANCELLING,
        _disconnect,
    ),
    ToolDefinition(
        "session_list",
        "List every SSH session without keys; states are starting, ready, lost, "
        "or closing.",
        EmptyInput,
        SessionListResponse,
        READ_ONLY,
        _session_list,
    ),
    ToolDefinition(
        "exec",
        "Run one bounded non-PTY command in one keyed session.",
        ExecInput,
        CommandResponse,
        MUTATING,
        _exec,
    ),
    ToolDefinition(
        "sudo_exec",
        "Run one command through passwordless-only sudo -n -k in one keyed session.",
        ExecInput,
        CommandResponse,
        MUTATING,
        _sudo_exec,
    ),
    ToolDefinition(
        "stat",
        "Inspect metadata for one remote path in one keyed session.",
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
        "Return metadata for one background transfer of the keyed session.",
        TransferIdInput,
        TransferResponse,
        READ_ONLY,
        _transfer_status,
    ),
    ToolDefinition(
        "transfer_cancel",
        "Cancel one transfer of the keyed session while preserving its partial.",
        TransferIdInput,
        TransferResponse,
        CANCELLING,
        _transfer_cancel,
    ),
    ToolDefinition(
        "transfer_list",
        "List retained background transfer metadata of the keyed session.",
        SessionInput,
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
        except ValidationError as error:
            payload = _error_response(
                definition.output_model,
                "invalid_arguments",
                _validation_message(definition.input_model, error),
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
        description="Explicitly connected, bounded operations over keyed SSH sessions.",
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
