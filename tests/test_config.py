from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import remote_ssh_mcp.config as config_module
from remote_ssh_mcp import machine
from remote_ssh_mcp.cli import build_parser
from remote_ssh_mcp.config import (
    ConnectionSpec,
    RuntimeConfig,
    runtime_repository_root,
    validate_ssh_alias,
    validate_ssh_host,
    validate_ssh_user,
)
from remote_ssh_mcp.errors import RemoteMCPError
from remote_ssh_mcp.mcp_models import ConnectInput


def namespace(*extra: str):
    return build_parser().parse_args([*extra])


def test_runtime_config_resolves_root_and_programs(tmp_path: Path) -> None:
    config = RuntimeConfig.from_namespace(namespace(), repository_root=tmp_path)

    assert config.repository_root == tmp_path.resolve()
    assert config.ssh_path.is_absolute()
    assert config.rsync_path.is_absolute()


@pytest.fixture(autouse=True)
def fixed_machine_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    identity = tmp_path / "machine-id"
    identity.write_text("0123456789abcdef" * 2 + "\n", encoding="ascii")
    monkeypatch.setattr(machine, "MACHINE_ID_FILE", identity)
    return identity


def _runtime_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    prefix = root / machine.repository_venv_root() / "venv-runtime"
    prefix.mkdir(parents=True)
    (root / ".version").write_text(f"{config_module.__version__}\n", encoding="utf-8")
    for name in ("requirements.txt", "remote-ssh-mcp"):
        (root / name).write_text("marker\n", encoding="utf-8")
    return root, prefix


def test_runtime_config_defaults_to_project_owning_active_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, prefix = _runtime_project(tmp_path)
    base_prefix = tmp_path / "base-python"
    base_prefix.mkdir()
    monkeypatch.setattr(config_module.sys, "prefix", str(prefix))
    monkeypatch.setattr(config_module.sys, "base_prefix", str(base_prefix))

    config = RuntimeConfig.from_namespace(namespace())

    assert config.repository_root == root


def test_runtime_root_rejects_non_virtual_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, prefix = _runtime_project(tmp_path)
    assert root.is_dir()
    monkeypatch.setattr(config_module.sys, "prefix", str(prefix))
    monkeypatch.setattr(config_module.sys, "base_prefix", str(prefix))

    with pytest.raises(RemoteMCPError, match="project-owned virtual environment"):
        runtime_repository_root()


def test_runtime_root_rejects_mismatched_project_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, prefix = _runtime_project(tmp_path)
    (root / ".version").write_text("999.0.0\n", encoding="utf-8")
    base_prefix = tmp_path / "base-python"
    base_prefix.mkdir()
    monkeypatch.setattr(config_module.sys, "prefix", str(prefix))
    monkeypatch.setattr(config_module.sys, "base_prefix", str(base_prefix))

    with pytest.raises(RemoteMCPError, match="package version does not match"):
        runtime_repository_root()


@pytest.mark.parametrize(
    ("layout", "message"),
    [
        ("legacy", "not a machine-scoped project environment"),
        ("other-machine", "belongs to another machine or user"),
        ("invalid-identity", "invalid or uninitialized"),
    ],
)
def test_runtime_root_accepts_only_this_machine_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixed_machine_identity: Path,
    layout: str,
    message: str,
) -> None:
    root, prefix = _runtime_project(tmp_path)
    if layout == "legacy":
        prefix = root / "venv-runtime"
    elif layout == "other-machine":
        prefix = root / ".venvs" / ("f" * 64) / "venv-runtime"
    else:
        fixed_machine_identity.write_text("uninitialized\n", encoding="ascii")
    prefix.mkdir(parents=True, exist_ok=True)
    base_prefix = tmp_path / "base-python"
    base_prefix.mkdir()
    monkeypatch.setattr(config_module.sys, "prefix", str(prefix))
    monkeypatch.setattr(config_module.sys, "base_prefix", str(base_prefix))

    with pytest.raises(RemoteMCPError, match=message) as raised:
        runtime_repository_root()
    assert raised.value.code == "invalid_configuration"
    assert "0123456789abcdef" not in raised.value.message


@pytest.mark.parametrize("invalid_marker", ["missing", "directory", "symlink"])
def test_runtime_root_rejects_unmarked_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_marker: str,
) -> None:
    root, prefix = _runtime_project(tmp_path)
    marker = root / "requirements.txt"
    marker.unlink()
    if invalid_marker == "directory":
        marker.mkdir()
    elif invalid_marker == "symlink":
        marker.symlink_to(root / ".version")
    base_prefix = tmp_path / "base-python"
    base_prefix.mkdir()
    monkeypatch.setattr(config_module.sys, "prefix", str(prefix))
    monkeypatch.setattr(config_module.sys, "base_prefix", str(base_prefix))

    with pytest.raises(RemoteMCPError, match="not owned by an MCP project"):
        runtime_repository_root()


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


def test_missing_repository_directory_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(RemoteMCPError, match="cannot be resolved"):
        RuntimeConfig.from_namespace(namespace(), repository_root=missing)


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o777])
def test_repository_uses_existing_filesystem_permissions(
    tmp_path: Path, mode: int
) -> None:
    tmp_path.chmod(mode)

    config = RuntimeConfig.from_namespace(namespace(), repository_root=tmp_path)

    assert config.repository_root == tmp_path.resolve()


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--connect-timeout", "0"),
        ("--command-timeout", "100000"),
        ("--max-output-bytes", "12"),
        ("--max-transfers", "0"),
        ("--max-transfers", "17"),
        ("--max-sessions", "0"),
        ("--max-sessions", "33"),
    ],
)
def test_limits_are_bounded(tmp_path: Path, option: str, value: str) -> None:
    with pytest.raises(RemoteMCPError, match="must be between"):
        RuntimeConfig.from_namespace(namespace(option, value), repository_root=tmp_path)


def test_session_and_transfer_limits_default_and_accept_bounds(tmp_path: Path) -> None:
    defaults = RuntimeConfig.from_namespace(namespace(), repository_root=tmp_path)
    assert defaults.max_sessions == 8
    assert defaults.max_transfers == 2

    bounded = RuntimeConfig.from_namespace(
        namespace("--max-sessions", "32", "--max-transfers", "16"),
        repository_root=tmp_path,
    )
    assert bounded.max_sessions == 32
    assert bounded.max_transfers == 16
