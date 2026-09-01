from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from ssh_wrapper import connection as wrapper_connection

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
import subprocess
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
    if os.environ.get("FAKE_SSH_MASTER_ENV"):
        Path(os.environ["FAKE_SSH_MASTER_ENV"]).write_text(
            os.environ.get("RECOVERED_SESSION_VALUE", "missing")
        )
    if os.environ.get("FAKE_SSH_MASTER_STDERR"):
        sys.stderr.write(os.environ["FAKE_SSH_MASTER_STDERR"])
        sys.stderr.flush()
    if os.environ.get("FAKE_SSH_MASTER_STDERR_BYTES"):
        remaining = int(os.environ["FAKE_SSH_MASTER_STDERR_BYTES"])
        while remaining:
            chunk = b"x" * min(8192, remaining)
            os.write(sys.stderr.fileno(), chunk)
            remaining -= len(chunk)
        os.write(sys.stderr.fileno(), b"MASTER_STDERR_END")
    if os.environ.get("FAKE_SSH_STDERR_HOLDER_PID"):
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        Path(os.environ["FAKE_SSH_STDERR_HOLDER_PID"]).write_text(str(holder.pid))
    if os.environ.get("FAKE_SSH_FAIL_MASTER") == "1":
        raise SystemExit(23)
    server = None
    if os.environ.get("FAKE_SSH_NO_SOCKET") != "1":
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
    if server is not None:
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


@pytest.fixture(autouse=True)
def inherited_session_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    async def inherited() -> dict[str, str]:
        return os.environ.copy()

    monkeypatch.setattr(wrapper_connection, "resolve_session_environment", inherited)


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
    stderr_task = master._stderr_task

    assert master.state is ConnectionState.READY
    assert stderr_task is not None and not stderr_task.done()
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
    assert stderr_task.done()
    assert master._stderr_task is None
    assert master._stderr_tail.data == b""
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
    stderr_task = master._stderr_task

    with pytest.raises(RemoteMCPError) as raised:
        await master.ensure_ready()
    assert raised.value.code == "connection_lost"
    assert count.read_text(encoding="utf-8") == "1"
    assert stderr_task is not None and stderr_task.done()
    assert master._stderr_task is None
    assert master._stderr_tail.data == b""

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
    assert master._stderr_task is None
    assert master._stderr_tail.data == b""
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


@pytest.mark.asyncio
async def test_recovered_environment_is_used_only_for_initial_master(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_record = tmp_path / "master-environment"
    monkeypatch.setenv("FAKE_SSH_MASTER_ENV", str(environment_record))
    resolved_calls = 0

    async def recovered() -> dict[str, str]:
        nonlocal resolved_calls
        resolved_calls += 1
        environment = os.environ.copy()
        environment["RECOVERED_SESSION_VALUE"] = "recovered"
        return environment

    monkeypatch.setattr(wrapper_connection, "resolve_session_environment", recovered)
    real_create_subprocess_exec = asyncio.create_subprocess_exec
    subprocess_environments: list[dict[str, str] | None] = []

    async def record_environment(
        *args: str, **kwargs: object
    ) -> asyncio.subprocess.Process:
        supplied = kwargs.get("env")
        assert supplied is None or isinstance(supplied, dict)
        subprocess_environments.append(supplied)
        return await real_create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(
        wrapper_connection.asyncio, "create_subprocess_exec", record_environment
    )
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )

    await master.start()
    await master.ensure_ready()
    await master.close()

    assert resolved_calls == 1
    assert environment_record.read_text(encoding="utf-8") == "recovered"
    assert subprocess_environments[0] is not None
    assert subprocess_environments[0]["RECOVERED_SESSION_VALUE"] == "recovered"
    assert all(environment is None for environment in subprocess_environments[1:])


@pytest.mark.asyncio
async def test_large_master_stderr_is_drained_and_bounded_while_ready(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_SSH_MASTER_STDERR_BYTES", str(512 * 1024))
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )

    await master.start()

    assert master.state is ConnectionState.READY
    assert len(master._stderr_tail.data) <= wrapper_connection.MASTER_STDERR_TAIL_BYTES
    assert master._stderr_tail.data.endswith(b"MASTER_STDERR_END")
    assert master._stderr_task is not None and not master._stderr_task.done()
    await master.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnostic_template",
    (
        (
            "load key {path}: secret detail\n"
            "read_passphrase: can't open /dev/tty: no device\n"
            "DISPLAY not set.\n"
        ),
        (
            'sign_and_send_pubkey: signing failed for ED25519-SK "{path}" '
            "from agent: agent refused operation\n"
        ),
    ),
)
async def test_askpass_failure_returns_stable_path_free_diagnostic(
    diagnostic_template: str,
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = "/private/identity/id_hardware"
    raw_diagnostic = diagnostic_template.format(path=private_path)
    monkeypatch.setenv("FAKE_SSH_MASTER_STDERR", raw_diagnostic)
    monkeypatch.setenv("FAKE_SSH_FAIL_MASTER", "1")
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )

    with pytest.raises(RemoteMCPError) as raised:
        await master.start()

    assert raised.value.message == wrapper_connection.INTERACTIVE_AUTHENTICATION_ERROR
    assert private_path not in raised.value.message
    assert raw_diagnostic not in raised.value.message
    assert master._stderr_task is None
    assert master._stderr_tail.data == b""


@pytest.mark.asyncio
async def test_unknown_master_failure_keeps_generic_status_only(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = "/private/identity/id_unknown"
    monkeypatch.setenv(
        "FAKE_SSH_MASTER_STDERR", f"proxy failed through {private_path}\n"
    )
    monkeypatch.setenv("FAKE_SSH_FAIL_MASTER", "1")
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )

    with pytest.raises(RemoteMCPError) as raised:
        await master.start()

    assert raised.value.message == "SSH master exited with status 23"
    assert private_path not in raised.value.message
    assert master._stderr_task is None
    assert master._stderr_tail.data == b""


@pytest.mark.asyncio
async def test_startup_failure_stops_a_stderr_holder_and_reaps_the_drain(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder_pid_path = tmp_path / "stderr-holder.pid"
    monkeypatch.setenv("FAKE_SSH_STDERR_HOLDER_PID", str(holder_pid_path))
    monkeypatch.setenv("FAKE_SSH_FAIL_MASTER", "1")
    monkeypatch.setattr(wrapper_connection, "MASTER_STDERR_DRAIN_TIMEOUT", 0.1)
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )

    with pytest.raises(RemoteMCPError):
        await master.start()

    holder_pid = int(holder_pid_path.read_text(encoding="utf-8"))
    async with asyncio.timeout(2):
        while True:
            try:
                os.kill(holder_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
    assert master._stderr_task is None
    assert master._stderr_tail.data == b""


@pytest.mark.asyncio
async def test_startup_timeout_stops_master_and_reaps_stderr(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_SSH_NO_SOCKET", "1")
    monkeypatch.setenv("FAKE_SSH_MASTER_STDERR", "waiting without a socket\n")
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh, connect_timeout=0.2),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )

    with pytest.raises(RemoteMCPError) as raised:
        await master.start()

    assert raised.value.message == (
        "SSH master did not become ready before the startup deadline"
    )
    assert master.process is not None and master.process.returncode is not None
    assert master._stderr_task is None
    assert master._stderr_tail.data == b""
    assert master.state is ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_cancelled_start_stops_master_and_reaps_stderr(
    runtime_config: RuntimeConfig,
    fake_ssh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_SSH_NO_SOCKET", "1")
    monkeypatch.setenv("FAKE_SSH_MASTER_STDERR", "waiting for cancellation\n")
    master = OpenSSHMaster(
        replace(runtime_config, ssh_path=fake_ssh),
        ConnectionSpec.from_alias("test-target"),
        runtime_base=tmp_path,
    )
    start_task = asyncio.create_task(master.start())
    async with asyncio.timeout(2):
        while master.process is None or master._stderr_task is None:
            await asyncio.sleep(0.01)
    stderr_task = master._stderr_task

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert master.process is not None and master.process.returncode is not None
    assert stderr_task is not None and stderr_task.done()
    assert master._stderr_task is None
    assert master._stderr_tail.data == b""
    assert master.state is ConnectionState.CLOSED
    assert master.runtime_dir is not None and not master.runtime_dir.exists()


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
