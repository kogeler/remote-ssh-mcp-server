"""Strictly non-interactive privileged command execution."""

from __future__ import annotations

import asyncio
import os
import secrets
import shlex
from pathlib import Path

from .commands import CapturedStream, CommandResult, CommandRunner
from .errors import RemoteMCPError

SUDO_REMOTE_PROGRAM = "env LC_ALL=C sudo -n -k -- /bin/bash --noprofile --norc -s"
MAX_DIAGNOSTIC_BYTES = 4096


def _remove_marker_from_file(path: Path, marker: bytes) -> None:
    """Remove one marker from a binary spool without loading it into memory."""

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    found = False
    pending = bytearray()
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
        )
        with path.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
            while chunk := source.read(65_536):
                pending.extend(chunk)
                if not found:
                    index = pending.find(marker)
                    if index >= 0:
                        destination.write(pending[:index])
                        del pending[: index + len(marker)]
                        found = True
                    elif len(pending) > len(marker):
                        safe_length = len(pending) - len(marker)
                        destination.write(pending[:safe_length])
                        del pending[:safe_length]
                else:
                    destination.write(pending)
                    pending.clear()
            destination.write(pending)
            destination.flush()
            os.fsync(destination.fileno())
        if found:
            os.replace(temporary, path)
        else:
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class SudoRunner:
    """Run a script only after passwordless, cache-independent sudo succeeds."""

    def __init__(
        self,
        runner: CommandRunner,
        *,
        remote_program: str = SUDO_REMOTE_PROGRAM,
    ) -> None:
        self.runner = runner
        self._remote_program = remote_program

    async def _strip_marker(
        self, stream: CapturedStream, marker: bytes
    ) -> CapturedStream:
        cleaned = stream.remove_once(marker)
        if stream.spool_path is not None:
            spool = self.runner.paths.root / stream.spool_path
            await asyncio.to_thread(_remove_marker_from_file, spool, marker)
        return cleaned

    @staticmethod
    def _classify_refusal(result: CommandResult) -> RemoteMCPError:
        diagnostic = result.stderr.text().strip()
        lowered = diagnostic.casefold()
        details = {
            "exit_code": result.exit_code,
            "diagnostic": diagnostic[:MAX_DIAGNOSTIC_BYTES],
        }

        if result.timed_out:
            return RemoteMCPError(
                "sudo_start_timeout",
                "sudo did not start a privileged command before the timeout",
                details,
            )
        if result.exit_code == 127 or ("sudo" in lowered and "not found" in lowered):
            return RemoteMCPError(
                "sudo_unavailable", "sudo is not available on the remote host", details
            )
        password_patterns = (
            "a password is required",
            "no tty present and no askpass program specified",
            "a terminal is required to read the password",
            "must have a tty to run sudo",
        )
        if any(pattern in lowered for pattern in password_patterns):
            return RemoteMCPError(
                "sudo_password_required",
                "sudo policy requires interactive authentication",
                details,
            )
        denied_patterns = (
            "not allowed to execute",
            "not in the sudoers",
            "may not run sudo",
            "is not allowed to run sudo",
        )
        if any(pattern in lowered for pattern in denied_patterns):
            return RemoteMCPError(
                "sudo_not_allowed",
                "sudo policy does not permit the requested privileged shell",
                details,
            )
        return RemoteMCPError(
            "sudo_refused",
            "sudo refused to start the privileged command non-interactively",
            details,
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

        marker_text = f"__REMOTE_SSH_MCP_SUDO_STARTED_{secrets.token_hex(24)}__"
        marker = f"{marker_text}\n".encode("ascii")
        script = f"printf '%s\\n' {shlex.quote(marker_text)} >&2\n{prefix}{command}"
        result = await self.runner.run_script(
            script,
            remote_program=self._remote_program,
            timeout=timeout,
            spool_output=spool_output,
        )

        if marker not in result.stderr.raw:
            raise self._classify_refusal(result)

        stderr = await self._strip_marker(result.stderr, marker)
        return CommandResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=stderr,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
        )
