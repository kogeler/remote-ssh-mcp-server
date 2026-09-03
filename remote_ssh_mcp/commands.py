"""Bounded, non-PTY command execution over an existing SSH master."""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RuntimeConfig
from .errors import RemoteMCPError
from .local_paths import LocalPathPolicy
from .master import OpenSSHMaster

MAX_COMMAND_BYTES = 1_048_576
PROCESS_TERM_TIMEOUT = 3.0
REMOTE_SHELL_PROGRAM = "/bin/sh -s"


def _supervised_remote_program(remote_program: str) -> str:
    """Wrap one trusted remote program with channel-loss process cleanup."""

    watcher = """\
group_pid=$1
payload_pipe=$2
watcher_gate=$3
watcher_pid_pipe=$4
runtime_dir=$5
IFS= read -r unexpected || true
/bin/kill -TERM -- "-$group_pid" 2>/dev/null || true
sleep 1
/bin/kill -KILL -- "-$group_pid" 2>/dev/null || true
rm -f -- "$payload_pipe"
rm -f -- "$watcher_gate"
rm -f -- "$watcher_pid_pipe"
rmdir -- "$runtime_dir" 2>/dev/null || true
"""
    watcher_launcher = f"""\
group_pid=$1
payload_pipe=$2
watcher_gate=$3
watcher_pid_pipe=$4
runtime_dir=$5
IFS= read -r armed < "$watcher_gate" || exit 75
rm -f -- "$watcher_gate"
exec setsid /bin/sh -c {shlex.quote(watcher)} remote-ssh-mcp-watcher \
    "$group_pid" "$payload_pipe" "$watcher_gate" "$watcher_pid_pipe" \
    "$runtime_dir"
"""
    child_runner = f"""\
payload_pipe=$1
watcher_gate=$2
watcher_pid_pipe=$3
runtime_dir=$4
shift 4
/bin/sh -c {shlex.quote(watcher_launcher)} remote-ssh-mcp-watcher-launcher "$$" \
    "$payload_pipe" "$watcher_gate" "$watcher_pid_pipe" "$runtime_dir" \
    <&3 >/dev/null 2>&1 &
watcher_pid=$!
printf '%s\n' "$watcher_pid" > "$watcher_pid_pipe" || exit 76
exec "$@"
"""
    supervisor = f"""\
umask 077
runtime_dir=
payload_pipe=
watcher_gate=
watcher_pid_pipe=
child_pid=
watcher_pid=
startup_critical=0
interruption_pending=0
terminate_child() {{
    if test -z "$child_pid"; then
        return
    fi
    if ! /bin/kill -TERM -- "-$child_pid" 2>/dev/null; then
        /bin/kill -TERM "$child_pid" 2>/dev/null || true
    fi
    /bin/kill -TERM -- "-$child_pid" 2>/dev/null || true
    sleep 1
    /bin/kill -KILL -- "-$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
    child_pid=
}}
stop_watcher() {{
    if test -z "$watcher_pid"; then
        return
    fi
    /bin/kill -TERM "$watcher_pid" 2>/dev/null || true
    watcher_pid=
}}
cleanup() {{
    terminate_child
    stop_watcher
    if test -n "$payload_pipe"; then
        rm -f -- "$payload_pipe"
    fi
    if test -n "$watcher_gate"; then
        rm -f -- "$watcher_gate"
    fi
    if test -n "$watcher_pid_pipe"; then
        rm -f -- "$watcher_pid_pipe"
    fi
    if test -n "$runtime_dir"; then
        rmdir -- "$runtime_dir" 2>/dev/null || true
    fi
}}
interrupted() {{
    if test "$startup_critical" = 1; then
        interruption_pending=1
        return
    fi
    terminate_child
    exit 255
}}
trap cleanup EXIT
trap interrupted HUP INT TERM
IFS= read -r payload_size || exit 70
case "$payload_size" in
    ''|*[!0-9]*) exit 71 ;;
esac
# Create the directory outside the supervisor's process group. The supervisor
# records cancellation during this short critical section, then either adopts
# the successfully created directory or exits without touching an existing path.
runtime_candidate=${{TMPDIR:-/tmp}}/remote-ssh-mcp.$$
startup_critical=1
if setsid mkdir -m 700 -- "$runtime_candidate"; then
    runtime_dir=$runtime_candidate
    runtime_status=0
else
    runtime_status=$?
fi
startup_critical=0
if test "$interruption_pending" = 1; then
    exit 255
fi
if test "$runtime_status" -ne 0; then
    exit 72
fi
payload_pipe=$runtime_dir/payload
watcher_gate=$runtime_dir/watcher-gate
watcher_pid_pipe=$runtime_dir/watcher-pid
mkfifo -m 600 "$payload_pipe" "$watcher_gate" "$watcher_pid_pipe" || exit 73
exec 3<&0
# Read-write guards make both control FIFOs non-blocking to open. The child
# closes these inherited descriptors and cannot keep either guard alive.
exec 7<> "$watcher_pid_pipe" || exit 73
exec 8<> "$watcher_gate" || exit 73
# A temporary read-write guard lets the supervisor open separate read-only and
# write-only ends without blocking. The child inherits only the read end, so it
# still receives EOF as soon as the payload writer closes.
exec 4<> "$payload_pipe" || exit 73
exec 5< "$payload_pipe" || exit 73
exec 6> "$payload_pipe" || exit 73
exec 4>&-
startup_critical=1
setsid /bin/sh -c {shlex.quote(child_runner)} remote-ssh-mcp-child \
    "$payload_pipe" "$watcher_gate" "$watcher_pid_pipe" "$runtime_dir" \
    {remote_program} \
    <&5 5<&- 6>&- 7>&- 8>&- &
child_pid=$!
startup_critical=0
if test "$interruption_pending" = 1; then
    exit 255
fi
exec 5<&-
# The child runner starts the watcher in the command's process group and then
# execs the remote shell. The watcher detaches only after this supervisor has
# its PID and arms it, so cancellation cannot strand an unregistered reader.
IFS= read -r watcher_pid <&7 || exit 76
exec 7>&-
case "$watcher_pid" in
    ''|*[!0-9]*) exit 76 ;;
esac
rm -f -- "$watcher_pid_pipe"
dd iflag=fullblock bs=1 count="$payload_size" status=none >&6 || exit 74
exec 6>&-
printf 'armed\n' >&8 || exit 75
exec 8>&-
wait "$child_pid"
status=$?
child_pid=
stop_watcher
exit "$status"
"""
    return shlex.join(("/bin/sh", "-c", supervisor))


@dataclass(frozen=True, slots=True)
class CapturedStream:
    raw: bytes
    total_bytes: int
    truncated: bool
    spool_path: str | None = None

    def text(self) -> str:
        return self.raw.decode("utf-8", errors="replace")

    def to_dict(self) -> dict[str, Any]:
        try:
            data = self.raw.decode("utf-8", errors="strict")
            encoding = "utf-8"
        except UnicodeDecodeError:
            data = base64.b64encode(self.raw).decode("ascii")
            encoding = "base64"
        return {
            "data": data,
            "encoding": encoding,
            "captured_bytes": len(self.raw),
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "spool_path": self.spool_path,
        }

    def remove_once(self, value: bytes) -> CapturedStream:
        index = self.raw.find(value)
        if index < 0:
            return self
        raw = self.raw[:index] + self.raw[index + len(value) :]
        return CapturedStream(
            raw=raw,
            total_bytes=max(0, self.total_bytes - len(value)),
            truncated=self.truncated,
            spool_path=self.spool_path,
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    stdout: CapturedStream
    stderr: CapturedStream
    timed_out: bool
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout.to_dict(),
            "stderr": self.stderr.to_dict(),
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
        }


class CommandRunner:
    def __init__(
        self,
        config: RuntimeConfig,
        master: OpenSSHMaster,
        paths: LocalPathPolicy,
    ) -> None:
        self.config = config
        self.master = master
        self.paths = paths
        self._processes: set[asyncio.subprocess.Process] = set()
        self._process_lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _validate_script(script: str) -> bytes:
        if not isinstance(script, str) or not script.strip():
            raise RemoteMCPError(
                "invalid_command", "command must be a non-empty string"
            )
        if "\x00" in script:
            raise RemoteMCPError("invalid_command", "command cannot contain a NUL byte")
        encoded = script.encode("utf-8")
        if len(encoded) > MAX_COMMAND_BYTES:
            raise RemoteMCPError(
                "invalid_command",
                f"command exceeds the {MAX_COMMAND_BYTES}-byte limit",
            )
        if not encoded.endswith(b"\n"):
            encoded += b"\n"
        return encoded

    async def _capture(
        self,
        reader: asyncio.StreamReader,
        limit: int,
        spool_path: Path | None,
    ) -> CapturedStream:
        captured = bytearray()
        total = 0
        spool = None
        display_path = None
        try:
            if spool_path is not None:
                descriptor = os.open(
                    spool_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
                    0o600,
                )
                spool = os.fdopen(descriptor, "wb")
                display_path = self.paths.display(spool_path)
            while True:
                chunk = await reader.read(65_536)
                if not chunk:
                    break
                total += len(chunk)
                if spool is not None:
                    spool.write(chunk)
                remaining = limit - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
        finally:
            if spool is not None:
                spool.flush()
                os.fsync(spool.fileno())
                spool.close()
        return CapturedStream(
            raw=bytes(captured),
            total_bytes=total,
            truncated=total > len(captured),
            spool_path=display_path,
        )

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

    async def run_script(
        self,
        script: str,
        *,
        remote_program: str = REMOTE_SHELL_PROGRAM,
        timeout: float | None = None,
        spool_output: bool = False,
    ) -> CommandResult:
        payload = self._validate_script(script)
        wire_payload = f"{len(payload)}\n".encode("ascii") + payload
        deadline = self.config.command_timeout if timeout is None else timeout
        if not 0.1 <= deadline <= 86_400:
            raise RemoteMCPError(
                "invalid_command",
                "command timeout must be between 0.1 and 86400 seconds",
            )

        await self.master.ensure_ready()
        stdout_spool = self.paths.new_spool_path("stdout") if spool_output else None
        stderr_spool = self.paths.new_spool_path("stderr") if spool_output else None
        started = time.monotonic()
        async with self._process_lock:
            if self._closed:
                raise RemoteMCPError(
                    "connection_lost", "SSH command runner has been disconnected"
                )
            process = await asyncio.create_subprocess_exec(
                *self.master.command_argv(_supervised_remote_program(remote_program)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self._processes.add(process)
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            self._capture(process.stdout, self.config.max_output_bytes, stdout_spool)
        )
        stderr_task = asyncio.create_task(
            self._capture(process.stderr, self.config.max_output_bytes, stderr_spool)
        )
        timed_out = False
        try:
            try:
                process.stdin.write(wire_payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=deadline)
            except TimeoutError:
                timed_out = True
                await self._terminate(process)
        except asyncio.CancelledError:
            await self._terminate(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        except BaseException:
            await self._terminate(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            async with self._process_lock:
                self._processes.discard(process)

        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        duration_ms = round((time.monotonic() - started) * 1000)
        result = CommandResult(
            exit_code=process.returncode if process.returncode is not None else 255,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )
        if self._closed:
            raise RemoteMCPError(
                "connection_lost", "SSH connection was explicitly disconnected"
            )
        if timed_out:
            return result
        if result.exit_code == 255:
            try:
                await self.master.ensure_ready()
            except RemoteMCPError as error:
                raise RemoteMCPError(
                    "connection_lost",
                    "SSH connection was lost while running a command",
                    {"stderr": stderr.text().strip()},
                ) from error
        return result

    async def close(self) -> None:
        async with self._process_lock:
            if self._closed:
                return
            self._closed = True
            processes = list(self._processes)
        if processes:
            await asyncio.gather(
                *(self._terminate(process) for process in processes),
                return_exceptions=True,
            )

    async def execute(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        spool_output: bool = False,
    ) -> CommandResult:
        prefix = ""
        if cwd is not None:
            if not cwd or "\x00" in cwd:
                raise RemoteMCPError(
                    "invalid_command", "remote working directory is invalid"
                )
            prefix = f"cd -- {shlex.quote(cwd)} || exit 125\n"
        return await self.run_script(
            prefix + command,
            timeout=timeout,
            spool_output=spool_output,
        )
