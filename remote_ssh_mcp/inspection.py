"""Small, bounded remote filesystem inspection helpers."""

from __future__ import annotations

import base64
import shlex
from typing import Any

from .commands import CommandRunner
from .errors import RemoteMCPError


def _remote_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RemoteMCPError("invalid_remote_path", "remote path must be non-empty")
    return value


def _encoded_bytes(value: bytes) -> dict[str, str]:
    try:
        return {"data": value.decode("utf-8", errors="strict"), "encoding": "utf-8"}
    except UnicodeDecodeError:
        return {"data": base64.b64encode(value).decode("ascii"), "encoding": "base64"}


class RemoteInspector:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    @staticmethod
    def _raise_command_error(path: str, stderr: str) -> None:
        raise RemoteMCPError(
            "remote_path_not_found",
            "remote path could not be inspected",
            {"path": path, "diagnostic": stderr.strip()},
        )

    async def stat(self, remote_path: str) -> dict[str, Any]:
        path = _remote_path(remote_path)
        format_string = "%f\t%s\t%Y\t%F"
        command = (
            f"LC_ALL=C stat -Lc {shlex.quote(format_string)} -- {shlex.quote(path)}"
        )
        result = await self.runner.run_script(command)
        if result.exit_code != 0:
            self._raise_command_error(path, result.stderr.text())
        fields = result.stdout.raw.rstrip(b"\n").split(b"\t")
        if len(fields) != 4:
            raise RemoteMCPError(
                "remote_protocol_error", "unexpected remote stat output"
            )
        try:
            return {
                "path": path,
                "mode": int(fields[0], 16),
                "size": int(fields[1]),
                "mtime": int(fields[2]),
                "type": fields[3].decode("utf-8", errors="replace"),
            }
        except ValueError as error:
            raise RemoteMCPError(
                "remote_protocol_error", "invalid numeric field in remote stat output"
            ) from error

    async def list_directory(self, remote_path: str) -> dict[str, Any]:
        path = _remote_path(remote_path)
        metadata = await self.stat(path)
        if metadata["type"] != "directory":
            raise RemoteMCPError(
                "invalid_remote_type", "remote listing source is not a directory"
            )
        format_string = "%f\\0%y\\0%s\\0%T@\\0"
        command = (
            f"find -- {shlex.quote(path)} -mindepth 1 -maxdepth 1 "
            f"-printf {shlex.quote(format_string)}"
        )
        result = await self.runner.run_script(command)
        if result.exit_code != 0:
            self._raise_command_error(path, result.stderr.text())
        if result.stdout.truncated:
            raise RemoteMCPError(
                "output_limit_exceeded",
                "remote directory listing exceeds the output limit",
            )
        fields = result.stdout.raw.split(b"\x00")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 4:
            raise RemoteMCPError(
                "remote_protocol_error", "unexpected remote directory listing output"
            )
        entries = []
        for index in range(0, len(fields), 4):
            try:
                size = int(fields[index + 2])
                mtime = float(fields[index + 3])
            except ValueError as error:
                raise RemoteMCPError(
                    "remote_protocol_error", "invalid directory listing metadata"
                ) from error
            entries.append(
                {
                    "name": _encoded_bytes(fields[index]),
                    "type": fields[index + 1].decode("ascii", errors="replace"),
                    "size": size,
                    "mtime": mtime,
                }
            )
        return {"path": path, "entries": entries, "count": len(entries)}

    async def read_file_range(
        self,
        remote_path: str,
        *,
        offset: int = 0,
        max_bytes: int = 65_536,
    ) -> dict[str, Any]:
        path = _remote_path(remote_path)
        if offset < 0:
            raise RemoteMCPError("invalid_range", "offset cannot be negative")
        if not 1 <= max_bytes <= self.runner.config.max_output_bytes:
            raise RemoteMCPError(
                "invalid_range",
                f"max_bytes must be between 1 and {self.runner.config.max_output_bytes}",
            )
        metadata = await self.stat(path)
        if metadata["type"] != "regular file":
            raise RemoteMCPError(
                "invalid_remote_type", "remote range source is not a regular file"
            )
        command = (
            f"dd if={shlex.quote(path)} bs=1 skip={offset} "
            f"count={max_bytes + 1} status=none"
        )
        result = await self.runner.run_script(command)
        if result.exit_code != 0:
            self._raise_command_error(path, result.stderr.text())
        data = result.stdout.raw[:max_bytes]
        encoded = _encoded_bytes(data)
        return {
            "path": path,
            "offset": offset,
            "bytes_read": len(data),
            "eof": result.stdout.total_bytes <= max_bytes,
            **encoded,
        }
