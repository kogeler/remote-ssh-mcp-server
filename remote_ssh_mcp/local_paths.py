"""Contain every model-selected local path below one configured root."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from .errors import RemoteMCPError

INTERNAL_DIRECTORY = ".remote-ssh-mcp"


class LocalPathPolicy:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise RemoteMCPError("invalid_local_path", "local root is not a directory")
        self.internal_root = self.root / INTERNAL_DIRECTORY

    def initialize(self) -> None:
        self._ensure_private_directory(self.internal_root)
        for name in ("spool", "partials"):
            self._ensure_private_directory(self.internal_root / name)

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            metadata = path.lstat()
        except OSError as error:
            raise RemoteMCPError(
                "invalid_local_path", "cannot inspect the internal tool directory"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise RemoteMCPError(
                "invalid_local_path",
                "the internal tool path must be a real directory",
            )
        if metadata.st_uid != os.getuid():
            raise RemoteMCPError(
                "invalid_local_path",
                "the internal tool directory must be owned by the current user",
            )
        os.chmod(path, 0o700, follow_symlinks=False)

    def _relative(self, value: str) -> Path:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise RemoteMCPError(
                "invalid_local_path", "local path must be a non-empty string"
            )
        path = Path(value)
        if path.is_absolute():
            raise RemoteMCPError(
                "invalid_local_path", "absolute local paths are not allowed"
            )
        if ".." in path.parts:
            raise RemoteMCPError(
                "invalid_local_path", "local path traversal is not allowed"
            )
        normalized = Path(*[part for part in path.parts if part not in ("", ".")])
        if not normalized.parts:
            raise RemoteMCPError(
                "invalid_local_path", "local path cannot resolve to the root"
            )
        if normalized.parts[0] == INTERNAL_DIRECTORY:
            raise RemoteMCPError(
                "invalid_local_path", "the internal tool directory is protected"
            )
        return normalized

    def _contained(self, path: Path) -> Path:
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise RemoteMCPError(
                "invalid_local_path", "local path escapes the configured root"
            ) from error
        return path

    def resolve_existing(self, value: str, *, require_file: bool = False) -> Path:
        relative = self._relative(value)
        try:
            resolved = (self.root / relative).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RemoteMCPError(
                "invalid_local_path",
                f"local path does not exist or cannot be resolved: {value}",
            ) from error
        self._contained(resolved)
        if require_file and not resolved.is_file():
            raise RemoteMCPError(
                "invalid_local_path", "local path is not a regular file"
            )
        return resolved

    def resolve_destination(self, value: str, *, overwrite: bool = False) -> Path:
        relative = self._relative(value)
        candidate = self.root / relative
        try:
            parent = candidate.parent.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RemoteMCPError(
                "invalid_local_path", "local destination parent does not exist"
            ) from error
        self._contained(parent)
        destination = parent / candidate.name

        if destination.is_symlink():
            raise RemoteMCPError(
                "invalid_local_path", "a local destination cannot be a symbolic link"
            )
        if destination.exists() and not overwrite:
            raise RemoteMCPError(
                "local_path_exists", "local destination already exists", {"path": value}
            )
        if destination.exists() and destination.is_dir():
            raise RemoteMCPError(
                "invalid_local_path", "local destination is an existing directory"
            )
        return destination

    def internal_path(self, category: str, token: str, suffix: str = "") -> Path:
        if category not in {"spool", "partials"}:
            raise ValueError(f"unsupported internal path category: {category}")
        if not token or any(character not in "0123456789abcdef" for character in token):
            raise ValueError("internal path token must be lowercase hexadecimal")
        path = self.internal_root / category / f"{token}{suffix}"
        return self._contained(path)

    def new_spool_path(self, stream_name: str) -> Path:
        if stream_name not in {"stdout", "stderr"}:
            raise ValueError("unsupported stream name")
        return self.internal_path("spool", secrets.token_hex(16), f".{stream_name}")

    def display(self, path: Path) -> str:
        return str(self._contained(path.resolve(strict=False)).relative_to(self.root))
