from __future__ import annotations

import os
import shlex
import signal
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from remote_ssh_mcp.config import ConnectionSpec, RuntimeConfig
from remote_ssh_mcp.errors import RemoteMCPError
from remote_ssh_mcp.master import (
    SSH_ISOLATION_OPTIONS,
    ConnectionState,
    OpenSSHMaster,
)

FAKE_SSH = r"""#!{python}
import os
import signal
import socket
import sys
import time
from pathlib import Path

args = sys.argv[1:]

def value(flag):
    return args[args.index(flag) + 1]

socket_path = Path(value("-S"))
pid_path = Path(str(socket_path) + ".pid")
count_path = os.environ.get("FAKE_SSH_AUTH_COUNT")

if "-O" in args:
    operation = value("-O")
    if operation == "check":
        raise SystemExit(0 if socket_path.exists() else 255)
    if operation == "exit":
        try:
            os.kill(int(pid_path.read_text()), signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError):
            raise SystemExit(255)
        raise SystemExit(0)

if "-M" in args and "-N" in args:
    if count_path:
        count = Path(count_path)
        previous = int(count.read_text()) if count.exists() else 0
        count.write_text(str(previous + 1))
    if os.environ.get("FAKE_SSH_FAIL_MASTER") == "1":
        raise SystemExit(23)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(socket_path))
    server.listen()
    pid_path.write_text(str(os.getpid()))
    stopping = False
    def stop(_signum, _frame):
        global stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        time.sleep(0.02)
    server.close()
    socket_path.unlink(missing_ok=True)
    pid_path.unlink(missing_ok=True)
    raise SystemExit(0)

raise SystemExit(99)
"""


@pytest.fixture
def fake_ssh(tmp_path: Path) -> Path:
    path = tmp_path / "fake-ssh"
    path.write_text(FAKE_SSH.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_master_authenticates_once_and_reuses_private_socket(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = tmp_path / "auth-count"
    monkeypatch.setenv("FAKE_SSH_AUTH_COUNT", str(count))
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )

    await master.start()
    await master.ensure_ready()
    await master.ensure_ready()

    assert master.state is ConnectionState.READY
    assert count.read_text(encoding="utf-8") == "1"
    assert master.runtime_dir is not None
    assert master.runtime_dir.stat().st_mode & 0o777 == 0o700
    assert master.control_path is not None and master.control_path.is_socket()

    transport = master.mux_transport_argv()
    assert "BatchMode=yes" in transport
    assert "PubkeyAuthentication=no" in transport
    assert f"ProxyCommand={runtime_config.false_path}" in transport
    assert "test-target" not in transport
    for option in SSH_ISOLATION_OPTIONS:
        assert option in master._master_argv()
        assert option in transport

    await master.close()
    assert master.state is ConnectionState.CLOSED
    assert master.runtime_dir is not None and not master.runtime_dir.exists()


@pytest.mark.asyncio
async def test_lost_master_never_restarts_authentication(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = tmp_path / "auth-count"
    monkeypatch.setenv("FAKE_SSH_AUTH_COUNT", str(count))
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )
    await master.start()
    assert master.process is not None

    os.killpg(master.process.pid, signal.SIGTERM)
    await master.process.wait()

    with pytest.raises(RemoteMCPError) as raised:
        await master.ensure_ready()
    assert raised.value.code == "connection_lost"
    assert count.read_text(encoding="utf-8") == "1"

    await master.close()
    assert count.read_text(encoding="utf-8") == "1"


@pytest.mark.asyncio
async def test_master_start_failure_cleans_runtime(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_SSH_FAIL_MASTER", "1")
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )

    with pytest.raises(RemoteMCPError) as raised:
        await master.start()

    assert raised.value.code == "connection_start_failed"
    assert master.state is ConnectionState.CLOSED
    assert master.runtime_dir is not None and not master.runtime_dir.exists()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_preserves_unrelated_files(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / "unrelated.sock"
    unrelated.write_text("owned elsewhere", encoding="utf-8")
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )
    await master.start()

    await master.close()
    await master.close()

    assert unrelated.read_text(encoding="utf-8") == "owned elsewhere"


def test_rsync_command_contains_only_mux_transport(
    runtime_config: RuntimeConfig, tmp_path: Path
) -> None:
    master = OpenSSHMaster(
        runtime_config,
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )
    master._create_runtime()
    try:
        command = master.rsync_ssh_command()
        assert "BatchMode=yes" in command
        assert "ProxyCommand=" in command
        assert "test-target" not in command
    finally:
        assert master.runtime_dir is not None
        master.runtime_dir.rmdir()


def test_direct_connection_values_are_separate_argv_elements(
    runtime_config: RuntimeConfig, tmp_path: Path
) -> None:
    connection = ConnectionSpec.from_direct("host.example", "deploy", 2222)
    master = OpenSSHMaster(runtime_config, connection, runtime_base=tmp_path)
    master._create_runtime()
    try:
        argv = master._master_argv()
        assert argv[-6:] == ["-l", "deploy", "-p", "2222", "--", "host.example"]
        command = master.command_argv("true")
        assert command[-7:] == [
            "-l",
            "deploy",
            "-p",
            "2222",
            "--",
            "host.example",
            "true",
        ]
        rsync_transport = shlex.split(master.rsync_ssh_command())
        assert rsync_transport[-4:] == ["-l", "deploy", "-p", "2222"]
        assert "host.example" not in rsync_transport
    finally:
        assert master.runtime_dir is not None
        master.runtime_dir.rmdir()
