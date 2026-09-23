"""Independent keyed SSH sessions owned by one MCP server process."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .commands import CommandRunner
from .config import ConnectionSpec, RuntimeConfig
from .errors import RemoteMCPError
from .inspection import RemoteInspector
from .local_paths import LocalPathPolicy
from .master import ConnectionState, OpenSSHMaster
from .sudo import SudoRunner
from .transfers import TransferManager, TransferReservations

SESSION_ID_BYTES = 16
SESSION_KEY_BYTES = 32
RESOLVE_TIMEOUT = 15.0
RESOLVE_OUTPUT_BYTES = 1_048_576
PROCESS_TERM_TIMEOUT = 3.0


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _key_digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """Effective OpenSSH destination which at most one session may own."""

    host: str
    port: int


def _resolution_error() -> RemoteMCPError:
    return RemoteMCPError(
        "connection_start_failed",
        "SSH configuration for the target could not be resolved",
    )


def _parse_server_identity(output: bytes) -> ServerIdentity:
    values: dict[str, str] = {}
    for line in output.decode("utf-8", errors="replace").splitlines():
        name, _separator, value = line.partition(" ")
        if name in {"hostname", "port"} and name not in values:
            values[name] = value.strip()
    host = values.get("hostname", "")
    port = values.get("port", "")
    if not host or any(character.isspace() for character in host):
        raise _resolution_error()
    if not port.isdigit() or not 1 <= int(port) <= 65_535:
        raise _resolution_error()
    return ServerIdentity(host=host.lower(), port=int(port))


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_TERM_TIMEOUT)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def resolve_server_identity(
    config: RuntimeConfig, connection: ConnectionSpec
) -> ServerIdentity:
    """Evaluate the effective OpenSSH HostName and Port without connecting."""
    try:
        process = await asyncio.create_subprocess_exec(
            str(config.ssh_path),
            "-G",
            *connection.ssh_options,
            "--",
            connection.destination,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise _resolution_error() from error
    assert process.stdout is not None
    output = bytearray()
    try:
        async with asyncio.timeout(RESOLVE_TIMEOUT):
            while chunk := await process.stdout.read(65_536):
                output.extend(chunk)
                if len(output) > RESOLVE_OUTPUT_BYTES:
                    raise _resolution_error()
            await process.wait()
    except TimeoutError as error:
        raise _resolution_error() from error
    finally:
        await _terminate(process)
    if process.returncode != 0:
        raise _resolution_error()
    return _parse_server_identity(bytes(output))


@dataclass(frozen=True, slots=True)
class SessionServices:
    runner: CommandRunner
    inspector: RemoteInspector
    sudo: SudoRunner
    transfers: TransferManager


@dataclass(eq=False, slots=True)
class RemoteSession:
    """One OpenSSH master and the services which may use it."""

    session_id: str
    server: ServerIdentity
    master: OpenSSHMaster
    key_digest: bytes = field(repr=False)
    created_at: str = field(default_factory=_timestamp)
    last_used_at: str | None = None
    services: SessionServices | None = field(default=None, repr=False)
    closing: bool = False
    start_task: asyncio.Task[None] | None = field(default=None, repr=False)
    close_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def accepts(self, key: str) -> bool:
        return hmac.compare_digest(_key_digest(key), self.key_digest)

    @property
    def state(self) -> str:
        state = self.master.state
        if state is ConnectionState.CLOSED:
            return "closed"
        if self.closing:
            return "closing"
        if state is ConnectionState.NEW:
            return "starting"
        return state.value

    def status(self) -> dict[str, Any]:
        """Return public metadata; the key and private paths never appear."""
        connection = self.master.connection
        process = self.master.process
        state = self.state
        services = self.services
        return {
            "session_id": self.session_id,
            "state": state,
            "mode": connection.mode.value,
            "target": connection.display_target,
            "ssh_alias": connection.ssh_alias,
            "host": connection.host,
            "user": connection.user,
            "port": connection.port,
            "server_host": self.server.host,
            "server_port": self.server.port,
            "master_pid": (
                process.pid if process is not None and state != "closed" else None
            ),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "active_commands": services.runner.active_count() if services else 0,
            "active_transfers": services.transfers.active_count() if services else 0,
        }


class SessionManager:
    """Own every keyed SSH session for one MCP server lifespan."""

    def __init__(self, config: RuntimeConfig, paths: LocalPathPolicy) -> None:
        self.config = config
        self.paths = paths
        self._sessions: dict[str, RemoteSession] = {}
        self._reservations = TransferReservations()
        self._authentication = asyncio.Lock()
        self._closed = False

    def _ensure_accepting(self) -> None:
        if self._closed:
            raise RemoteMCPError("server_closing", "the MCP server is shutting down")
        if len(self._sessions) >= self.config.max_sessions:
            raise RemoteMCPError(
                "session_limit_reached",
                f"at most {self.config.max_sessions} SSH sessions may be open; "
                "disconnect an unused session first",
            )

    def _new_session_id(self) -> str:
        while True:
            candidate = secrets.token_hex(SESSION_ID_BYTES)
            if candidate not in self._sessions:
                return candidate

    def _owner(self, server: ServerIdentity) -> RemoteSession | None:
        for session in self._sessions.values():
            if session.server == server:
                return session
        return None

    async def connect(self, connection: ConnectionSpec) -> tuple[RemoteSession, str]:
        """Authenticate a new session and return it with its only key copy."""
        self._ensure_accepting()
        server = await resolve_server_identity(self.config, connection)
        while (owner := self._owner(server)) is not None and owner.closing:
            await self._close(owner)
        if owner is not None:
            raise RemoteMCPError(
                "already_connected",
                f"SSH session {owner.session_id} already owns this server; "
                "disconnect it before connecting again",
            )
        self._ensure_accepting()

        key = secrets.token_urlsafe(SESSION_KEY_BYTES)
        session = RemoteSession(
            session_id=self._new_session_id(),
            server=server,
            master=OpenSSHMaster(self.config, connection),
            key_digest=_key_digest(key),
        )
        self._sessions[session.session_id] = session
        start = asyncio.create_task(self._start(session))
        session.start_task = start
        try:
            await asyncio.wait((start,))
        except asyncio.CancelledError:
            await self._close(session)
            raise
        if session.closing or start.cancelled():
            await self._close(session)
            raise RemoteMCPError(
                "session_closed",
                "the SSH session was disconnected before it became ready",
            )
        error = start.exception()
        if error is not None:
            await self._close(session)
            raise error
        return session, key

    async def _start(self, session: RemoteSession) -> None:
        # One authentication prompt at a time keeps hardware-key touches and
        # system dialogs attributable to exactly one pending session.
        async with self._authentication:
            await session.master.start()
        runner = CommandRunner(self.config, session.master, self.paths)
        inspector = RemoteInspector(runner)
        session.services = SessionServices(
            runner=runner,
            inspector=inspector,
            sudo=SudoRunner(runner),
            transfers=TransferManager(
                self.config,
                session.master,
                self.paths,
                runner,
                inspector,
                reservations=self._reservations,
            ),
        )

    def authorize(
        self, session_id: str, session_key: str
    ) -> tuple[RemoteSession, SessionServices]:
        """Return a ready session only for the exact key issued by connect."""
        session = self._sessions.get(session_id)
        if session is None:
            raise RemoteMCPError("session_not_found", "SSH session was not found")
        if not session.accepts(session_key):
            raise RemoteMCPError(
                "invalid_session_key", "session key does not match this SSH session"
            )
        if session.closing:
            raise RemoteMCPError("session_closing", "SSH session is being disconnected")
        services = session.services
        if services is None:
            raise RemoteMCPError(
                "session_not_ready", "SSH session is still authenticating"
            )
        session.last_used_at = _timestamp()
        return session, services

    async def _refresh(self, session: RemoteSession) -> None:
        if (
            session.closing
            or session.services is None
            or session.master.state is not ConnectionState.READY
        ):
            return
        try:
            await session.master.ensure_ready()
        except RemoteMCPError:
            pass

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Return public metadata for every session without any key."""
        sessions = list(self._sessions.values())
        await asyncio.gather(*(self._refresh(session) for session in sessions))
        return [
            session.status()
            for session in sessions
            if self._sessions.get(session.session_id) is session
        ]

    async def disconnect(self, session_id: str) -> dict[str, Any]:
        """Close any session by its public ID; no key is required."""
        session = self._sessions.get(session_id)
        if session is None:
            raise RemoteMCPError("session_not_found", "SSH session was not found")
        await self._close(session)
        return session.status()

    async def _close(self, session: RemoteSession) -> None:
        if session.close_task is None:
            session.closing = True
            session.close_task = asyncio.create_task(self._shutdown(session))
        # A cancelled caller must not abandon cleanup halfway through.
        await asyncio.shield(session.close_task)

    async def _shutdown(self, session: RemoteSession) -> None:
        try:
            start = session.start_task
            if start is not None and not start.done():
                start.cancel()
                await asyncio.wait((start,))
            services = session.services
            try:
                if services is not None:
                    await services.transfers.close()
            finally:
                try:
                    if services is not None:
                        await services.runner.close()
                finally:
                    await session.master.close()
        finally:
            self._sessions.pop(session.session_id, None)

    async def close(self) -> None:
        """Refuse new sessions, then close every owned session."""
        self._closed = True
        sessions = list(self._sessions.values())
        results = await asyncio.gather(
            *(self._close(session) for session in sessions), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
