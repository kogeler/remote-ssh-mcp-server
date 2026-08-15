"""Immutable startup configuration and validation."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shutil
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .errors import RemoteMCPError

SSH_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")
SSH_HOST_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}\Z")
SSH_USER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}\Z")
MIN_TIMEOUT = 0.1
MAX_CONNECT_TIMEOUT = 900.0
MAX_COMMAND_TIMEOUT = 86_400.0
MIN_OUTPUT_BYTES = 1_024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_TRANSFERS = 16


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


def validate_ssh_alias(value: str) -> str:
    if not SSH_ALIAS_PATTERN.fullmatch(value):
        raise RemoteMCPError(
            "invalid_connection",
            "ssh_alias must use only letters, digits, '.', '_', and '-' and cannot start with '-'",
        )
    return value


def validate_ssh_host(value: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not SSH_HOST_PATTERN.fullmatch(value):
            raise RemoteMCPError(
                "invalid_connection",
                "host must be an IPv4 address, an unbracketed IPv6 address, or a conservative DNS name",
            ) from None
    return value


def validate_ssh_user(value: str) -> str:
    if not SSH_USER_PATTERN.fullmatch(value):
        raise RemoteMCPError(
            "invalid_connection",
            "user must use only letters, digits, '.', '_', '+', and '-' and cannot start with '-'",
        )
    return value


class ConnectionMode(StrEnum):
    ALIAS = "alias"
    DIRECT = "direct"


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    mode: ConnectionMode
    ssh_alias: str | None = None
    host: str | None = None
    user: str | None = None
    port: int | None = None

    @classmethod
    def from_alias(cls, ssh_alias: str) -> ConnectionSpec:
        return cls(
            mode=ConnectionMode.ALIAS,
            ssh_alias=validate_ssh_alias(ssh_alias),
        )

    @classmethod
    def from_direct(cls, host: str, user: str, port: int = 22) -> ConnectionSpec:
        if not 1 <= port <= 65_535:
            raise RemoteMCPError(
                "invalid_connection", "port must be between 1 and 65535"
            )
        return cls(
            mode=ConnectionMode.DIRECT,
            host=validate_ssh_host(host),
            user=validate_ssh_user(user),
            port=port,
        )

    @property
    def destination(self) -> str:
        if self.mode is ConnectionMode.ALIAS:
            assert self.ssh_alias is not None
            return self.ssh_alias
        assert self.host is not None
        return self.host

    @property
    def ssh_options(self) -> tuple[str, ...]:
        if self.mode is ConnectionMode.ALIAS:
            return ()
        assert self.user is not None and self.port is not None
        return ("-l", self.user, "-p", str(self.port))

    @property
    def rsync_target(self) -> str:
        destination = self.destination
        if self.mode is ConnectionMode.DIRECT and ":" in destination:
            return f"[{destination}]"
        return destination

    @property
    def cache_key(self) -> str:
        if self.mode is ConnectionMode.ALIAS:
            return f"alias:{self.destination}"
        assert self.user is not None and self.port is not None
        return f"direct:{self.user}@{self.destination}:{self.port}"

    @property
    def display_target(self) -> str:
        if self.mode is ConnectionMode.ALIAS:
            return self.destination
        assert self.user is not None and self.port is not None
        host = f"[{self.destination}]" if ":" in self.destination else self.destination
        return f"{self.user}@{host}:{self.port}"

    def status(self, state: str, master_pid: int | None) -> dict[str, str | int | None]:
        return {
            "state": state,
            "mode": self.mode.value,
            "target": self.display_target,
            "ssh_alias": self.ssh_alias,
            "host": self.host,
            "user": self.user,
            "port": self.port,
            "master_pid": master_pid,
        }


def resolve_program(name: str) -> Path:
    resolved = shutil.which(name)
    if resolved is None:
        raise RemoteMCPError(
            "missing_dependency",
            f"required command not found on PATH: {name}",
            {"command": name},
        )
    return Path(resolved).resolve(strict=True)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    local_root: Path
    connect_timeout: float
    command_timeout: float
    max_output_bytes: int
    max_transfers: int
    log_level: str
    ssh_path: Path
    rsync_path: Path
    false_path: Path

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> RuntimeConfig:
        root_input = Path(args.local_root).expanduser()
        if not root_input.is_absolute():
            raise RemoteMCPError(
                "invalid_configuration", "local root must be an absolute path"
            )
        try:
            local_root = root_input.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RemoteMCPError(
                "invalid_configuration",
                f"local root cannot be resolved: {error}",
            ) from error
        if not local_root.is_dir():
            raise RemoteMCPError(
                "invalid_configuration", "local root must be an existing directory"
            )
        root_metadata = local_root.stat()
        if root_metadata.st_uid != os.getuid():
            raise RemoteMCPError(
                "invalid_configuration", "local root must be owned by the current user"
            )
        if root_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RemoteMCPError(
                "invalid_configuration",
                "local root must not be writable by group or other users",
            )

        return cls(
            local_root=local_root,
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
            log_level=args.log_level,
            ssh_path=resolve_program("ssh"),
            rsync_path=resolve_program("rsync"),
            false_path=resolve_program("false"),
        )
