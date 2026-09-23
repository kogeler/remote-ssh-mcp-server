"""Immutable startup configuration and validation."""

from __future__ import annotations

import argparse
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from ssh_wrapper.connection import (
    ConnectionMode,
    ConnectionSpec,
    resolve_program,
    validate_ssh_alias,
    validate_ssh_host,
    validate_ssh_user,
)

from . import __version__
from .errors import RemoteMCPError
from .machine import VENV_NAMESPACE_DIRECTORY, MachineIdentityError, environment_key

__all__ = [
    "ConnectionMode",
    "ConnectionSpec",
    "RuntimeConfig",
    "resolve_program",
    "runtime_repository_root",
    "validate_ssh_alias",
    "validate_ssh_host",
    "validate_ssh_user",
]

MIN_TIMEOUT = 0.1
MAX_CONNECT_TIMEOUT = 900.0
MAX_COMMAND_TIMEOUT = 86_400.0
MIN_OUTPUT_BYTES = 1_024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_TRANSFERS = 16
MAX_SESSIONS = 32
_RUNTIME_ROOT_MARKERS = (".version", "requirements.txt", "remote-ssh-mcp")


def runtime_repository_root() -> Path:
    """Resolve the project owning the active venv in this machine's namespace."""
    try:
        prefix = Path(sys.prefix).resolve(strict=True)
        base_prefix = Path(sys.base_prefix).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RemoteMCPError(
            "invalid_configuration",
            "the active Python environment cannot be resolved",
        ) from error
    if prefix == base_prefix:
        raise RemoteMCPError(
            "invalid_configuration",
            "remote-ssh-mcp requires a project-owned virtual environment",
        )

    namespace = prefix.parent
    if namespace.parent.name != VENV_NAMESPACE_DIRECTORY:
        raise RemoteMCPError(
            "invalid_configuration",
            "the active virtual environment is not a machine-scoped project environment",
        )
    try:
        key = environment_key()
    except MachineIdentityError as error:
        raise RemoteMCPError("invalid_configuration", str(error)) from error
    if namespace.name != key:
        raise RemoteMCPError(
            "invalid_configuration",
            "the active virtual environment belongs to another machine or user",
        )
    root = namespace.parent.parent
    for name in _RUNTIME_ROOT_MARKERS:
        try:
            mode = (root / name).lstat().st_mode
        except OSError as error:
            raise RemoteMCPError(
                "invalid_configuration",
                "the active virtual environment is not owned by an MCP project",
            ) from error
        if not stat.S_ISREG(mode):
            raise RemoteMCPError(
                "invalid_configuration",
                "the active virtual environment is not owned by an MCP project",
            )
    try:
        project_version = (root / ".version").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise RemoteMCPError(
            "invalid_configuration",
            "the MCP project version cannot be read",
        ) from error
    if project_version != __version__:
        raise RemoteMCPError(
            "invalid_configuration",
            "the active MCP package version does not match its project",
        )
    return root


def _bounded_float(name: str, value: float, minimum: float, maximum: float) -> float:
    if not minimum <= value <= maximum:
        raise RemoteMCPError(
            "invalid_configuration",
            f"{name} must be between {minimum:g} and {maximum:g}",
        )
    return value


def _bounded_int(name: str, value: int, minimum: int, maximum: int) -> int:
    if not minimum <= value <= maximum:
        raise RemoteMCPError(
            "invalid_configuration",
            f"{name} must be between {minimum} and {maximum}",
        )
    return value


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    repository_root: Path
    connect_timeout: float
    command_timeout: float
    max_output_bytes: int
    max_transfers: int
    max_sessions: int
    log_level: str
    ssh_path: Path
    rsync_path: Path
    false_path: Path

    @classmethod
    def from_namespace(
        cls,
        args: argparse.Namespace,
        *,
        repository_root: Path | None = None,
    ) -> RuntimeConfig:
        root_input = (
            runtime_repository_root() if repository_root is None else repository_root
        )
        try:
            resolved_root = root_input.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RemoteMCPError(
                "invalid_configuration",
                f"local root cannot be resolved: {error}",
            ) from error
        if not resolved_root.is_dir():
            raise RemoteMCPError(
                "invalid_configuration",
                "local root must be an existing directory",
            )

        return cls(
            repository_root=resolved_root,
            connect_timeout=_bounded_float(
                "connect timeout",
                args.connect_timeout,
                MIN_TIMEOUT,
                MAX_CONNECT_TIMEOUT,
            ),
            command_timeout=_bounded_float(
                "command timeout",
                args.command_timeout,
                MIN_TIMEOUT,
                MAX_COMMAND_TIMEOUT,
            ),
            max_output_bytes=_bounded_int(
                "max output bytes",
                args.max_output_bytes,
                MIN_OUTPUT_BYTES,
                MAX_OUTPUT_BYTES,
            ),
            max_transfers=_bounded_int(
                "max transfers", args.max_transfers, 1, MAX_TRANSFERS
            ),
            max_sessions=_bounded_int(
                "max sessions", args.max_sessions, 1, MAX_SESSIONS
            ),
            log_level=args.log_level,
            ssh_path=resolve_program("ssh"),
            rsync_path=resolve_program("rsync"),
            false_path=resolve_program("false"),
        )
