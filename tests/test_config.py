from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from remote_ssh_mcp.cli import build_parser
from remote_ssh_mcp.config import (
    ConnectionSpec,
    RuntimeConfig,
    validate_ssh_alias,
    validate_ssh_host,
    validate_ssh_user,
)
from remote_ssh_mcp.errors import RemoteMCPError
from remote_ssh_mcp.mcp_models import ConnectInput


def namespace(root: Path, *extra: str):
    return build_parser().parse_args(["--local-root", str(root), *extra])


def test_runtime_config_resolves_root_and_programs(tmp_path: Path) -> None:
    config = RuntimeConfig.from_namespace(namespace(tmp_path))

    assert config.local_root == tmp_path.resolve()
    assert config.ssh_path.is_absolute()
    assert config.rsync_path.is_absolute()


@pytest.mark.parametrize(
    "ssh_alias",
    ["-host", "host name", "host:/path", "user@host", "host/other", "", "host$bad"],
)
def test_alias_rejects_ambiguous_or_unsafe_tokens(ssh_alias: str) -> None:
    with pytest.raises(RemoteMCPError, match="ssh_alias must"):
        validate_ssh_alias(ssh_alias)


@pytest.mark.parametrize("host", ["-host", "host name", "user@host", "host/path"])
def test_direct_host_rejects_unsafe_tokens(host: str) -> None:
    with pytest.raises(RemoteMCPError, match="host must"):
        validate_ssh_host(host)


@pytest.mark.parametrize("user", ["-root", "user name", "user@host", "root/other"])
def test_direct_user_rejects_unsafe_tokens(user: str) -> None:
    with pytest.raises(RemoteMCPError, match="user must"):
        validate_ssh_user(user)


def test_direct_connection_supports_ipv6_and_separate_ssh_options() -> None:
    connection = ConnectionSpec.from_direct("2001:db8::7", "deploy", 2222)

    assert connection.destination == "2001:db8::7"
    assert connection.rsync_target == "[2001:db8::7]"
    assert connection.ssh_options == ("-l", "deploy", "-p", "2222")


def test_connect_input_modes_are_exclusive_and_direct_port_defaults_to_22() -> None:
    direct = ConnectInput(host="host.example", user="deploy").connection_spec()
    assert direct.port == 22

    with pytest.raises(ValidationError):
        ConnectInput(ssh_alias="configured", host="host.example", user="deploy")
    with pytest.raises(ValidationError):
        ConnectInput(host="host.example")


def test_relative_local_root_is_rejected(tmp_path: Path) -> None:
    args = namespace(tmp_path)
    args.local_root = "relative"

    with pytest.raises(RemoteMCPError, match="absolute"):
        RuntimeConfig.from_namespace(args)


@pytest.mark.parametrize("mode", [0o720, 0o702])
def test_local_root_rejects_group_or_other_writes(tmp_path: Path, mode: int) -> None:
    tmp_path.chmod(mode)

    with pytest.raises(RemoteMCPError, match="must not be writable"):
        RuntimeConfig.from_namespace(namespace(tmp_path))


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--connect-timeout", "0"),
        ("--command-timeout", "100000"),
        ("--max-output-bytes", "12"),
        ("--max-transfers", "0"),
        ("--max-transfers", "17"),
    ],
)
def test_limits_are_bounded(tmp_path: Path, option: str, value: str) -> None:
    with pytest.raises(RemoteMCPError, match="must be between"):
        RuntimeConfig.from_namespace(namespace(tmp_path, option, value))
