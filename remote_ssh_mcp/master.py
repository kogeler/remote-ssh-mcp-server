"""Lifecycle for one owned native OpenSSH multiplexing master."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import stat
import tempfile
from enum import StrEnum
from pathlib import Path

from .config import ConnectionSpec, RuntimeConfig
from .errors import RemoteMCPError

CONTROL_CHECK_TIMEOUT = 5.0
PROCESS_STOP_TIMEOUT = 5.0
MAX_CONTROL_PATH_BYTES = 96

SSH_ISOLATION_OPTIONS = (
    "ClearAllForwardings=yes",
    "ForwardAgent=no",
    "ForwardX11=no",
    "ForwardX11Trusted=no",
    "PermitLocalCommand=no",
    "RemoteCommand=none",
)


class ConnectionState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    LOST = "lost"
    CLOSING = "closing"
    CLOSED = "closed"


class OpenSSHMaster:
    """Own exactly one OpenSSH master process and its private mux socket."""

    def __init__(
        self,
        config: RuntimeConfig,
        connection: ConnectionSpec,
        *,
        runtime_base: Path | None = None,
    ) -> None:
        self.config = config
        self.connection = connection
        self._runtime_base = runtime_base
        self.runtime_dir: Path | None = None
        self.control_path: Path | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.state = ConnectionState.NEW

    def _select_runtime_base(self) -> Path:
        if self._runtime_base is not None:
            base = self._runtime_base.resolve(strict=True)
        else:
            configured = os.environ.get("XDG_RUNTIME_DIR")
            if configured:
                candidate = Path(configured)
                try:
                    base = candidate.resolve(strict=True)
                    if not base.is_dir() or base.stat().st_uid != os.getuid():
                        raise OSError("XDG_RUNTIME_DIR is not an owned directory")
                except OSError:
                    base = Path(tempfile.gettempdir()).resolve(strict=True)
            else:
                base = Path(tempfile.gettempdir()).resolve(strict=True)
        if not base.is_dir() or not os.access(base, os.W_OK | os.X_OK):
            raise RemoteMCPError(
                "connection_start_failed", "no writable runtime directory is available"
            )
        return base

    def _create_runtime(self) -> None:
        base = self._select_runtime_base()
        prefix = f"remote-ssh-mcp-{os.getuid()}-"
        runtime = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        os.chmod(runtime, 0o700)
        control = runtime / "mux.sock"

        if len(os.fsencode(control)) > MAX_CONTROL_PATH_BYTES:
            shutil.rmtree(runtime)
            fallback = Path(tempfile.gettempdir()).resolve(strict=True)
            runtime = Path(tempfile.mkdtemp(prefix=f"rsm-{os.getuid()}-", dir=fallback))
            os.chmod(runtime, 0o700)
            control = runtime / "m"
        if len(os.fsencode(control)) > MAX_CONTROL_PATH_BYTES:
            shutil.rmtree(runtime)
            raise RemoteMCPError(
                "connection_start_failed",
                "cannot create a short enough SSH control path",
            )

        self.runtime_dir = runtime
        self.control_path = control

    def _master_argv(self) -> list[str]:
        if self.control_path is None:
            raise RuntimeError("runtime directory is not initialized")
        return [
            str(self.config.ssh_path),
            "-M",
            "-N",
            "-S",
            str(self.control_path),
            "-o",
            "ControlMaster=yes",
            "-o",
            "ControlPersist=no",
            *(item for option in SSH_ISOLATION_OPTIONS for item in ("-o", option)),
            "-o",
            f"ConnectTimeout={max(1, int(self.config.connect_timeout))}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            *self.connection.ssh_options,
            "--",
            self.connection.destination,
        ]

    def mux_transport_argv(self) -> list[str]:
        """Return ssh plus fixed options, without destination or remote command.

        ProxyCommand points to ``false`` so a missing mux socket cannot fall
        back to a new TCP connection. Authentication methods are disabled as a
        second independent barrier.
        """

        if self.control_path is None:
            raise RemoteMCPError("connection_lost", "SSH master has no control socket")
        return [
            str(self.config.ssh_path),
            "-T",
            "-S",
            str(self.control_path),
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPersist=no",
            "-o",
            "BatchMode=yes",
            "-o",
            "NumberOfPasswordPrompts=0",
            *(item for option in SSH_ISOLATION_OPTIONS for item in ("-o", option)),
            "-o",
            f"ProxyCommand={self.config.false_path}",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "GSSAPIAuthentication=no",
            "-o",
            "HostbasedAuthentication=no",
            *self.connection.ssh_options,
        ]

    def command_argv(self, remote_program: str) -> list[str]:
        return [
            *self.mux_transport_argv(),
            "--",
            self.connection.destination,
            remote_program,
        ]

    def rsync_ssh_command(self) -> str:
        return shlex.join(self.mux_transport_argv())

    async def _control_operation(self, operation: str) -> tuple[int, bytes, bytes]:
        if self.control_path is None:
            return 255, b"", b"control socket is not initialized"
        process = await asyncio.create_subprocess_exec(
            str(self.config.ssh_path),
            "-S",
            str(self.control_path),
            "-O",
            operation,
            *self.connection.ssh_options,
            "--",
            self.connection.destination,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=CONTROL_CHECK_TIMEOUT
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return 255, b"", b"control operation timed out"
        return process.returncode or 0, stdout, stderr

    def _socket_exists(self) -> bool:
        if self.control_path is None:
            return False
        try:
            return stat.S_ISSOCK(self.control_path.stat().st_mode)
        except OSError:
            return False

    async def start(self) -> None:
        if self.state is not ConnectionState.NEW:
            raise RemoteMCPError(
                "connection_start_failed", "SSH master can only be started once"
            )
        self.state = ConnectionState.STARTING
        self._create_runtime()

        try:
            self.process = await asyncio.create_subprocess_exec(
                *self._master_argv(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=None,
                start_new_session=True,
            )
            deadline = asyncio.get_running_loop().time() + self.config.connect_timeout
            last_error = b""
            while asyncio.get_running_loop().time() < deadline:
                if self.process.returncode is not None:
                    raise RemoteMCPError(
                        "connection_start_failed",
                        f"SSH master exited with status {self.process.returncode}",
                    )
                if self._socket_exists():
                    returncode, _stdout, last_error = await self._control_operation(
                        "check"
                    )
                    if returncode == 0:
                        self.state = ConnectionState.READY
                        return
                await asyncio.sleep(0.1)
            message = "SSH master did not become ready before the startup deadline"
            if last_error:
                message += f": {last_error.decode('utf-8', errors='replace').strip()}"
            raise RemoteMCPError("connection_start_failed", message)
        except BaseException:
            await self.close()
            raise

    async def ensure_ready(self) -> None:
        if self.state is not ConnectionState.READY:
            raise RemoteMCPError(
                "connection_lost", f"SSH master is not ready (state: {self.state})"
            )
        if (
            self.process is None
            or self.process.returncode is not None
            or not self._socket_exists()
        ):
            self.state = ConnectionState.LOST
            raise RemoteMCPError(
                "connection_lost", "SSH master process or socket is gone"
            )
        returncode, _stdout, stderr = await self._control_operation("check")
        if returncode != 0:
            self.state = ConnectionState.LOST
            raise RemoteMCPError(
                "connection_lost",
                "SSH master control check failed",
                {"diagnostic": stderr.decode("utf-8", errors="replace").strip()},
            )

    async def _stop_process(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.process.wait(), timeout=PROCESS_STOP_TIMEOUT)
        except TimeoutError:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.wait()

    async def close(self) -> None:
        if self.state is ConnectionState.CLOSED:
            return
        self.state = ConnectionState.CLOSING
        if (
            self.process is not None
            and self.process.returncode is None
            and self._socket_exists()
        ):
            await self._control_operation("exit")
            try:
                await asyncio.wait_for(
                    self.process.wait(), timeout=PROCESS_STOP_TIMEOUT
                )
            except TimeoutError:
                await self._stop_process()
        else:
            await self._stop_process()

        runtime = self.runtime_dir
        if runtime is not None:
            try:
                if runtime.stat().st_uid == os.getuid():
                    shutil.rmtree(runtime)
            except FileNotFoundError:
                pass
        self.state = ConnectionState.CLOSED

    def status(self) -> dict[str, str | int | None]:
        return self.connection.status(
            self.state.value,
            self.process.pid if self.process is not None else None,
        )
