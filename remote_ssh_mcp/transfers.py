"""Resumable single-file transfers over the owned SSH multiplexing master."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import shutil
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .commands import CommandRunner
from .config import RuntimeConfig
from .errors import RemoteMCPError
from .inspection import RemoteInspector
from .local_paths import LocalPathPolicy
from .master import OpenSSHMaster

RSYNC_TAIL_BYTES = 65_536
PROCESS_TERM_TIMEOUT = 5.0
DEFAULT_COMPLETED_TTL = 3600.0
HASH_PATTERN = re.compile(rb"\A([0-9a-fA-F]{64})[ \t]")
PROGRESS_PATTERN = re.compile(rb"(?:^|[\r\n])\s*([0-9][0-9,]*)\s+[0-9]+%")


class TransferDirection(StrEnum):
    DOWNLOAD = "download"
    UPLOAD = "upload"


class TransferState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


FINAL_STATES = {
    TransferState.COMPLETED,
    TransferState.FAILED,
    TransferState.CANCELLED,
}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _remote_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RemoteMCPError(
            "invalid_remote_path", "remote path must be a non-empty string"
        )
    return value


def _remote_destination(value: str) -> str:
    path = _remote_path(value)
    if path.endswith("/") or path in {".", ".."}:
        raise RemoteMCPError(
            "invalid_remote_path", "remote destination must name a file"
        )
    return path


class _HashCancelled(Exception):
    pass


def _sha256_file(path: Path, cancel_event: threading.Event) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            if cancel_event.is_set():
                raise _HashCancelled
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class TransferOperation:
    operation_id: str
    direction: TransferDirection
    local_path: str
    remote_path: str
    overwrite: bool
    state: TransferState = TransferState.PENDING
    created_at: str = field(default_factory=_timestamp)
    started_at: str | None = None
    finished_at: str | None = None
    bytes_transferred: int = 0
    total_bytes: int | None = None
    sha256: str | None = None
    exit_code: int | None = None
    stdout_tail: bytes = b""
    stderr_tail: bytes = b""
    error: dict[str, Any] | None = None
    _resource_key: str = field(default="", repr=False)
    _local_absolute: Path | None = field(default=None, repr=False)
    _partial_path: Path | None = field(default=None, repr=False)
    _remote_partial: str | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _finished_monotonic: float | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "direction": self.direction.value,
            "state": self.state.value,
            "local_path": self.local_path,
            "remote_path": self.remote_path,
            "overwrite": self.overwrite,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "bytes_transferred": self.bytes_transferred,
            "total_bytes": self.total_bytes,
            "sha256": self.sha256,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail.decode("utf-8", errors="replace"),
            "stderr_tail": self.stderr_tail.decode("utf-8", errors="replace"),
            "error": self.error,
        }


class TransferManager:
    def __init__(
        self,
        config: RuntimeConfig,
        master: OpenSSHMaster,
        paths: LocalPathPolicy,
        runner: CommandRunner,
        inspector: RemoteInspector,
        *,
        completed_ttl: float = DEFAULT_COMPLETED_TTL,
    ) -> None:
        self.config = config
        self.master = master
        self.paths = paths
        self.runner = runner
        self.inspector = inspector
        self.completed_ttl = completed_ttl
        self._operations: dict[str, TransferOperation] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            operation_id
            for operation_id, operation in self._operations.items()
            if operation._finished_monotonic is not None
            and now - operation._finished_monotonic >= self.completed_ttl
        ]
        for operation_id in expired:
            del self._operations[operation_id]

    def _active_count(self) -> int:
        return sum(
            operation.state not in FINAL_STATES
            for operation in self._operations.values()
        )

    def _register(self, operation: TransferOperation) -> None:
        if self._closed:
            raise RemoteMCPError(
                "transfer_manager_closed", "transfer manager is closed"
            )
        self._prune()
        if self._active_count() >= self.config.max_transfers:
            raise RemoteMCPError(
                "transfer_limit_reached",
                f"at most {self.config.max_transfers} transfers may run concurrently",
            )
        if any(
            existing.state not in FINAL_STATES
            and existing._resource_key == operation._resource_key
            for existing in self._operations.values()
        ):
            raise RemoteMCPError(
                "transfer_conflict",
                "another active transfer already owns the same destination",
            )
        self._operations[operation.operation_id] = operation

    @staticmethod
    def _partial_token(*values: str) -> str:
        digest = hashlib.sha256()
        for value in values:
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()[:32]

    async def start_download(
        self,
        remote_path: str,
        local_path: str,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        remote = _remote_path(remote_path)
        destination = self.paths.resolve_destination(local_path, overwrite=overwrite)
        display = self.paths.display(destination)
        await self.master.ensure_ready()

        operation = TransferOperation(
            operation_id=secrets_token(),
            direction=TransferDirection.DOWNLOAD,
            local_path=display,
            remote_path=remote,
            overwrite=overwrite,
            _resource_key=f"download\0{display}",
            _local_absolute=destination,
        )
        token = self._partial_token(
            "download", self.master.connection.cache_key, remote, display
        )
        operation._partial_path = self.paths.internal_path(
            "partials", token, ".download"
        )
        async with self._lock:
            self._register(operation)
            operation._task = asyncio.create_task(self._run_download(operation))
        return operation.to_dict()

    async def start_upload(
        self,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        source = self.paths.resolve_existing(local_path, require_file=True)
        remote = _remote_destination(remote_path)
        display = self.paths.display(source)
        await self.master.ensure_ready()

        operation = TransferOperation(
            operation_id=secrets_token(),
            direction=TransferDirection.UPLOAD,
            local_path=display,
            remote_path=remote,
            overwrite=overwrite,
            _resource_key=f"upload\0{remote}",
            _local_absolute=source,
        )
        token = self._partial_token(
            "upload", self.master.connection.cache_key, display, remote
        )
        operation._remote_partial = f"{remote}.remote-ssh-mcp-{token}.partial"
        async with self._lock:
            self._register(operation)
            operation._task = asyncio.create_task(self._run_upload(operation))
        return operation.to_dict()

    @staticmethod
    def _set_state(operation: TransferOperation, state: TransferState) -> None:
        operation.state = state
        if state is TransferState.RUNNING and operation.started_at is None:
            operation.started_at = _timestamp()
        if state in FINAL_STATES:
            operation.finished_at = _timestamp()
            operation._finished_monotonic = time.monotonic()

    async def _read_rsync_stream(
        self,
        reader: asyncio.StreamReader,
        operation: TransferOperation,
        *,
        parse_progress: bool,
    ) -> bytes:
        tail = bytearray()
        progress_window = bytearray()
        while chunk := await reader.read(65_536):
            tail.extend(chunk)
            if len(tail) > RSYNC_TAIL_BYTES:
                del tail[: len(tail) - RSYNC_TAIL_BYTES]
            if parse_progress:
                progress_window.extend(chunk)
                if len(progress_window) > RSYNC_TAIL_BYTES:
                    del progress_window[: len(progress_window) - RSYNC_TAIL_BYTES]
                matches = PROGRESS_PATTERN.findall(progress_window)
                if matches:
                    operation.bytes_transferred = int(matches[-1].replace(b",", b""))
        return bytes(tail)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=PROCESS_TERM_TIMEOUT)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def _run_rsync(
        self,
        operation: TransferOperation,
        source: str,
        destination: str,
    ) -> None:
        await self.master.ensure_ready()
        argv = [
            str(self.config.rsync_path),
            "--partial",
            "--append-verify",
            "--protect-args",
            "--info=progress2",
            "--no-motd",
            "-e",
            self.master.rsync_ssh_command(),
            "--",
            source,
            destination,
        ]
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        operation._process = process
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            self._read_rsync_stream(process.stdout, operation, parse_progress=True)
        )
        stderr_task = asyncio.create_task(
            self._read_rsync_stream(process.stderr, operation, parse_progress=False)
        )
        try:
            await process.wait()
        except asyncio.CancelledError:
            await self._terminate(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        except BaseException:
            await self._terminate(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            operation._process = None

        operation.stdout_tail, operation.stderr_tail = await asyncio.gather(
            stdout_task, stderr_task
        )
        operation.exit_code = process.returncode
        if process.returncode != 0:
            try:
                await self.master.ensure_ready()
            except RemoteMCPError as error:
                raise RemoteMCPError(
                    "connection_lost", "SSH connection was lost during transfer"
                ) from error
            raise RemoteMCPError(
                "transfer_failed",
                f"rsync exited with status {process.returncode}",
                {
                    "exit_code": process.returncode,
                    "diagnostic": operation.stderr_tail.decode(
                        "utf-8", errors="replace"
                    ),
                },
            )

    async def _remote_sha256(self, remote_path: str) -> str:
        command = f"LC_ALL=C sha256sum --zero -- {shlex.quote(remote_path)}"
        result = await self.runner.run_script(command)
        if result.exit_code != 0:
            code = (
                "remote_hash_unavailable"
                if result.exit_code == 127
                else "remote_hash_failed"
            )
            raise RemoteMCPError(
                code,
                "remote SHA-256 could not be computed",
                {
                    "exit_code": result.exit_code,
                    "diagnostic": result.stderr.text().strip(),
                },
            )
        match = HASH_PATTERN.match(result.stdout.raw)
        if match is None:
            raise RemoteMCPError(
                "remote_protocol_error", "unexpected remote sha256sum output"
            )
        return match.group(1).decode("ascii").lower()

    @staticmethod
    def _validate_partial(path: Path, remote_size: int) -> int:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return 0
        if path.is_symlink() or not path.is_file() or metadata.st_uid != os.getuid():
            raise RemoteMCPError(
                "invalid_transfer_partial", "download partial is not a safe owned file"
            )
        if metadata.st_size > remote_size:
            path.unlink()
            return 0
        return metadata.st_size

    async def _run_download(self, operation: TransferOperation) -> None:
        self._set_state(operation, TransferState.RUNNING)
        assert operation._local_absolute is not None
        assert operation._partial_path is not None
        partial_path = operation._partial_path
        try:
            metadata = await self.inspector.stat(operation.remote_path)
            if metadata["type"] != "regular file":
                raise RemoteMCPError(
                    "invalid_remote_type", "download source is not a regular file"
                )
            operation.total_bytes = metadata["size"]
            partial_size = self._validate_partial(partial_path, operation.total_bytes)
            operation.bytes_transferred = partial_size
            free_bytes = await asyncio.to_thread(
                lambda: shutil.disk_usage(partial_path.parent).free
            )
            required_bytes = max(0, operation.total_bytes - partial_size)
            if free_bytes < required_bytes:
                raise RemoteMCPError(
                    "insufficient_local_space",
                    "local root does not have enough free space for the download",
                    {"required_bytes": required_bytes, "free_bytes": free_bytes},
                )

            source = f"{self.master.connection.rsync_target}:{operation.remote_path}"
            await self._run_rsync(operation, source, str(partial_path))
            operation.bytes_transferred = partial_path.stat().st_size
            self._set_state(operation, TransferState.VERIFYING)
            remote_hash = await self._remote_sha256(operation.remote_path)
            local_hash = await asyncio.to_thread(
                _sha256_file, partial_path, operation._cancel_event
            )
            if local_hash != remote_hash:
                raise RemoteMCPError(
                    "verification_failed",
                    "download SHA-256 does not match the remote source",
                )

            destination = self.paths.resolve_destination(
                operation.local_path, overwrite=operation.overwrite
            )
            try:
                if operation.overwrite:
                    os.replace(operation._partial_path, destination)
                else:
                    os.link(operation._partial_path, destination)
                    operation._partial_path.unlink()
            except FileExistsError as error:
                raise RemoteMCPError(
                    "local_path_exists", "local destination already exists"
                ) from error
            except OSError as error:
                raise RemoteMCPError(
                    "transfer_publish_failed",
                    f"verified download could not be published: {error.strerror}",
                ) from error
            operation.sha256 = local_hash
            operation.bytes_transferred = operation.total_bytes
            self._set_state(operation, TransferState.COMPLETED)
        except asyncio.CancelledError:
            if operation._partial_path.exists() and operation._partial_path.is_file():
                operation.bytes_transferred = operation._partial_path.stat().st_size
            self._set_state(operation, TransferState.CANCELLED)
        except RemoteMCPError as error:
            if operation._partial_path.exists() and operation._partial_path.is_file():
                operation.bytes_transferred = operation._partial_path.stat().st_size
            operation.error = error.to_dict()
            self._set_state(operation, TransferState.FAILED)
        except Exception:  # noqa: BLE001 - background failures become operation state.
            operation.error = RemoteMCPError(
                "transfer_internal_error", "unexpected local transfer failure"
            ).to_dict()
            self._set_state(operation, TransferState.FAILED)

    async def _remote_exists(self, remote_path: str) -> bool:
        quoted = shlex.quote(remote_path)
        result = await self.runner.run_script(f"test -e {quoted} || test -L {quoted}")
        return result.exit_code == 0

    async def _prepare_remote_partial(
        self, remote_partial: str, source_size: int
    ) -> None:
        quoted = shlex.quote(remote_partial)
        script = (
            f"if test -L {quoted}; then exit 74; fi\n"
            f"if test -e {quoted} && ! test -f {quoted}; then exit 75; fi\n"
            f"if test -f {quoted}; then\n"
            f"  size=$(LC_ALL=C stat -Lc %s -- {quoted}) || exit 76\n"
            f'  if test "$size" -gt {source_size}; then rm -f -- {quoted} || exit 77; fi\n'
            "fi"
        )
        result = await self.runner.run_script(script)
        if result.exit_code != 0:
            raise RemoteMCPError(
                "invalid_transfer_partial",
                "remote upload partial is not a safe regular file",
                {
                    "exit_code": result.exit_code,
                    "diagnostic": result.stderr.text().strip(),
                },
            )

    async def _publish_upload(self, operation: TransferOperation) -> None:
        assert operation._remote_partial is not None
        partial = shlex.quote(operation._remote_partial)
        final = shlex.quote(operation.remote_path)
        if operation.overwrite:
            command = f"mv -fT -- {partial} {final}"
        else:
            command = f"ln -- {partial} {final} && rm -f -- {partial}"
        result = await self.runner.run_script(command)
        if result.exit_code == 0:
            return
        if not operation.overwrite and await self._remote_exists(operation.remote_path):
            raise RemoteMCPError(
                "remote_path_exists", "remote destination already exists"
            )
        raise RemoteMCPError(
            "transfer_publish_failed",
            "verified upload could not be published",
            {"exit_code": result.exit_code, "diagnostic": result.stderr.text().strip()},
        )

    async def _run_upload(self, operation: TransferOperation) -> None:
        self._set_state(operation, TransferState.RUNNING)
        assert operation._local_absolute is not None
        assert operation._remote_partial is not None
        try:
            metadata = operation._local_absolute.stat()
            if not operation._local_absolute.is_file():
                raise RemoteMCPError(
                    "invalid_local_path", "upload source is not a regular file"
                )
            operation.total_bytes = metadata.st_size
            if not operation.overwrite and await self._remote_exists(
                operation.remote_path
            ):
                raise RemoteMCPError(
                    "remote_path_exists", "remote destination already exists"
                )
            await self._prepare_remote_partial(
                operation._remote_partial, operation.total_bytes
            )
            local_hash = await asyncio.to_thread(
                _sha256_file, operation._local_absolute, operation._cancel_event
            )
            destination = (
                f"{self.master.connection.rsync_target}:{operation._remote_partial}"
            )
            await self._run_rsync(
                operation, str(operation._local_absolute), destination
            )
            operation.bytes_transferred = operation.total_bytes
            self._set_state(operation, TransferState.VERIFYING)
            remote_hash = await self._remote_sha256(operation._remote_partial)
            if local_hash != remote_hash:
                raise RemoteMCPError(
                    "verification_failed",
                    "upload SHA-256 does not match the local source",
                )
            await self._publish_upload(operation)
            operation.sha256 = local_hash
            self._set_state(operation, TransferState.COMPLETED)
        except asyncio.CancelledError:
            self._set_state(operation, TransferState.CANCELLED)
        except RemoteMCPError as error:
            operation.error = error.to_dict()
            self._set_state(operation, TransferState.FAILED)
        except Exception:  # noqa: BLE001 - background failures become operation state.
            operation.error = RemoteMCPError(
                "transfer_internal_error", "unexpected remote transfer failure"
            ).to_dict()
            self._set_state(operation, TransferState.FAILED)

    async def status(self, operation_id: str) -> dict[str, Any]:
        async with self._lock:
            self._prune()
            operation = self._operations.get(operation_id)
            if operation is None:
                raise RemoteMCPError(
                    "transfer_not_found", "transfer operation was not found"
                )
            if (
                operation.direction is TransferDirection.DOWNLOAD
                and operation.state not in FINAL_STATES
                and operation._partial_path is not None
            ):
                try:
                    operation.bytes_transferred = operation._partial_path.stat().st_size
                except OSError:
                    pass
            return operation.to_dict()

    async def list(self) -> list[dict[str, Any]]:
        async with self._lock:
            self._prune()
            return [operation.to_dict() for operation in self._operations.values()]

    async def cancel(self, operation_id: str) -> dict[str, Any]:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise RemoteMCPError(
                    "transfer_not_found", "transfer operation was not found"
                )
            task = operation._task
            if operation.state in FINAL_STATES or task is None:
                return operation.to_dict()
            operation._cancel_event.set()
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if operation.state not in FINAL_STATES:
            self._set_state(operation, TransferState.CANCELLED)
        return operation.to_dict()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = [
                operation._task
                for operation in self._operations.values()
                if operation._task is not None and operation.state not in FINAL_STATES
            ]
            for operation in self._operations.values():
                if operation._task in tasks:
                    operation._cancel_event.set()
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for operation in self._operations.values():
            if operation._task in tasks and operation.state not in FINAL_STATES:
                self._set_state(operation, TransferState.CANCELLED)


def secrets_token() -> str:
    return os.urandom(16).hex()
