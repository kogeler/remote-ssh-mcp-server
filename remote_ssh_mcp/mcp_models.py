"""Strict MCP input and output models."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .commands import MAX_COMMAND_BYTES
from .config import MAX_COMMAND_TIMEOUT, MAX_OUTPUT_BYTES, ConnectionSpec
from .errors import RemoteMCPError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


RemotePath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_COMMAND_BYTES,
        pattern=r"^[^\x00]+$",
        description="Path on the fixed remote host",
    ),
]
LocalPath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=16_384,
        pattern=r"^[^\x00]+$",
        description="Relative path below the configured local root",
    ),
]
OperationId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Timeout = Annotated[float, Field(ge=0.1, le=MAX_COMMAND_TIMEOUT)]
SSHAlias = Annotated[
    str,
    Field(
        min_length=1,
        max_length=255,
        description="Host alias from the user's standard OpenSSH config",
    ),
]
SSHHost = Annotated[
    str,
    Field(
        min_length=1,
        max_length=253,
        description="DNS name, IPv4 address, or unbracketed IPv6 address",
    ),
]
SSHUser = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        description="SSH user for direct host mode",
    ),
]
SSHPort = Annotated[
    int,
    Field(ge=1, le=65_535, description="SSH port for direct mode; defaults to 22"),
]


class EmptyInput(StrictModel):
    pass


class ConnectInput(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["ssh_alias"],
                    "properties": {
                        "ssh_alias": {"type": "string"},
                        "host": {"type": "null"},
                        "user": {"type": "null"},
                        "port": {"type": "null"},
                    },
                },
                {
                    "required": ["host", "user"],
                    "properties": {
                        "ssh_alias": {"type": "null"},
                        "host": {"type": "string"},
                        "user": {"type": "string"},
                    },
                },
            ]
        },
    )
    ssh_alias: SSHAlias | None = None
    host: SSHHost | None = None
    user: SSHUser | None = None
    port: SSHPort | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> ConnectInput:
        try:
            self.connection_spec()
        except RemoteMCPError as error:
            raise ValueError(error.message) from None
        return self

    def connection_spec(self) -> ConnectionSpec:
        if self.ssh_alias is not None:
            if self.host is not None or self.user is not None or self.port is not None:
                raise RemoteMCPError(
                    "invalid_connection",
                    "ssh_alias cannot be combined with host, user, or port",
                )
            return ConnectionSpec.from_alias(self.ssh_alias)
        if self.host is None or self.user is None:
            raise RemoteMCPError(
                "invalid_connection",
                "provide either ssh_alias or both host and user",
            )
        return ConnectionSpec.from_direct(self.host, self.user, self.port or 22)


class ExecInput(StrictModel):
    command: Annotated[str, Field(min_length=1, max_length=MAX_COMMAND_BYTES)]
    cwd: RemotePath | None = None
    timeout: Timeout | None = None
    spool_output: bool = False


class StatInput(StrictModel):
    remote_path: RemotePath


class ListDirectoryInput(StrictModel):
    remote_path: RemotePath


class ReadFileRangeInput(StrictModel):
    remote_path: RemotePath
    offset: Annotated[int, Field(ge=0)] = 0
    max_bytes: Annotated[int, Field(ge=1, le=MAX_OUTPUT_BYTES)] = 65_536


class DownloadStartInput(StrictModel):
    remote_path: RemotePath
    local_path: LocalPath
    overwrite: bool = False


class UploadStartInput(StrictModel):
    local_path: LocalPath
    remote_path: RemotePath
    overwrite: bool = False


class TransferIdInput(StrictModel):
    operation_id: OperationId


class PublicError(StrictModel):
    code: str
    message: str


class EncodedData(StrictModel):
    data: str
    encoding: str


class CapturedStreamData(EncodedData):
    captured_bytes: int
    total_bytes: int
    truncated: bool
    spool_path: str | None = None


class CommandData(StrictModel):
    exit_code: int
    stdout: CapturedStreamData
    stderr: CapturedStreamData
    timed_out: bool
    duration_ms: int


class ConnectionData(StrictModel):
    state: str
    mode: str | None
    target: str | None
    ssh_alias: str | None
    host: str | None
    user: str | None
    port: int | None
    master_pid: int | None


class StatData(StrictModel):
    path: str
    mode: int
    size: int
    mtime: int
    type: str


class DirectoryEntry(StrictModel):
    name: EncodedData
    type: str
    size: int
    mtime: float


class DirectoryData(StrictModel):
    path: str
    entries: list[DirectoryEntry]
    count: int


class ReadFileRangeData(EncodedData):
    path: str
    offset: int
    bytes_read: int
    eof: bool


class TransferData(StrictModel):
    operation_id: OperationId
    direction: str
    state: str
    local_path: str
    remote_path: str
    overwrite: bool
    created_at: str
    started_at: str | None
    finished_at: str | None
    bytes_transferred: int
    total_bytes: int | None
    sha256: str | None
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    error: PublicError | None


class ConnectionResponse(StrictModel):
    ok: bool
    result: ConnectionData | None = None
    error: PublicError | None = None


class CommandResponse(StrictModel):
    ok: bool
    result: CommandData | None = None
    error: PublicError | None = None


class StatResponse(StrictModel):
    ok: bool
    result: StatData | None = None
    error: PublicError | None = None


class DirectoryResponse(StrictModel):
    ok: bool
    result: DirectoryData | None = None
    error: PublicError | None = None


class ReadFileRangeResponse(StrictModel):
    ok: bool
    result: ReadFileRangeData | None = None
    error: PublicError | None = None


class TransferResponse(StrictModel):
    ok: bool
    result: TransferData | None = None
    error: PublicError | None = None


class TransferListResponse(StrictModel):
    ok: bool
    result: list[TransferData] | None = None
    error: PublicError | None = None
