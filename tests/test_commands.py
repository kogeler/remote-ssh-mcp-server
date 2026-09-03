from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
from pathlib import Path

import pytest

from remote_ssh_mcp.commands import CommandRunner
from remote_ssh_mcp.config import RuntimeConfig
from remote_ssh_mcp.errors import RemoteMCPError
from remote_ssh_mcp.inspection import RemoteInspector
from remote_ssh_mcp.local_paths import LocalPathPolicy


class LocalShellMaster:
    def __init__(self) -> None:
        self.ready_checks = 0
        self.fail = False

    async def ensure_ready(self) -> None:
        self.ready_checks += 1
        if self.fail:
            raise RemoteMCPError("connection_lost", "fake master is gone")

    def command_argv(self, remote_program: str) -> list[str]:
        return shlex.split(remote_program)


@pytest.fixture
def command_stack(
    runtime_config: RuntimeConfig,
) -> tuple[CommandRunner, RemoteInspector, LocalShellMaster, LocalPathPolicy]:
    paths = LocalPathPolicy(runtime_config.repository_root)
    paths.initialize()
    master = LocalShellMaster()
    runner = CommandRunner(runtime_config, master, paths)  # type: ignore[arg-type]
    return runner, RemoteInspector(runner), master, paths


@pytest.mark.asyncio
async def test_command_separates_streams_and_exit_code(command_stack) -> None:
    runner, _inspector, master, _paths = command_stack

    result = await runner.execute("printf output; printf error >&2; exit 7")

    assert result.exit_code == 7
    assert result.stdout.raw == b"output"
    assert result.stderr.raw == b"error"
    assert not result.timed_out
    assert master.ready_checks == 1


@pytest.mark.asyncio
async def test_signal_exit_is_preserved(command_stack) -> None:
    runner, _inspector, _master, _paths = command_stack

    result = await runner.execute("kill -TERM $$", timeout=10)

    assert result.exit_code == 128 + signal.SIGTERM


@pytest.mark.asyncio
async def test_commands_do_not_share_shell_state(command_stack) -> None:
    runner, _inspector, _master, _paths = command_stack

    first = await runner.execute("cd /; export REMOTE_MCP_LEAK=yes")
    second = await runner.execute("printf '%s' \"${REMOTE_MCP_LEAK-unset}\"")

    assert first.exit_code == 0
    assert second.stdout.raw == b"unset"


@pytest.mark.asyncio
async def test_command_uses_requested_working_directory(
    command_stack, tmp_path: Path
) -> None:
    runner, _inspector, _master, _paths = command_stack
    directory = tmp_path / "directory with spaces"
    directory.mkdir()

    result = await runner.execute("pwd", cwd=str(directory))

    assert result.stdout.text().strip() == str(directory)


@pytest.mark.asyncio
async def test_timeout_terminates_process_group(command_stack) -> None:
    runner, _inspector, _master, _paths = command_stack

    result = await runner.execute("sleep 10", timeout=0.1)

    assert result.timed_out
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_output_is_truncated_and_optionally_spooled(
    command_stack,
) -> None:
    runner, _inspector, _master, paths = command_stack
    command = f"head -c {runner.config.max_output_bytes + 1000} /dev/zero"

    result = await runner.execute(command, spool_output=True)

    assert result.stdout.truncated
    assert len(result.stdout.raw) == runner.config.max_output_bytes
    assert result.stdout.total_bytes == runner.config.max_output_bytes + 1000
    assert result.stdout.spool_path is not None
    spool = paths.repository / result.stdout.spool_path
    assert spool.stat().st_size == result.stdout.total_bytes
    assert spool.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_binary_output_is_base64_in_serialized_result(command_stack) -> None:
    runner, _inspector, _master, _paths = command_stack

    result = await runner.execute("printf '\\377'")

    assert result.stdout.raw == b"\xff"
    assert result.stdout.to_dict()["encoding"] == "base64"


@pytest.mark.asyncio
async def test_cancelled_command_reaps_child(command_stack, tmp_path: Path) -> None:
    runner, _inspector, _master, _paths = command_stack
    pid_file = tmp_path / "remote-command.pid"
    task = asyncio.create_task(
        runner.execute(f"echo $$ > {shlex.quote(str(pid_file))}; exec sleep 10")
    )
    async with asyncio.timeout(2):
        while not pid_file.exists():
            await asyncio.sleep(0.01)
    pid = int(pid_file.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(3):
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_remote_supervisor_removes_private_runtime(
    command_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _inspector, _master, _paths = command_stack
    runtime = tmp_path / "remote-runtime"
    runtime.mkdir()
    monkeypatch.setenv("TMPDIR", str(runtime))

    result = await runner.execute("printf supervised")

    assert result.exit_code == 0
    assert result.stdout.raw == b"supervised"
    assert list(runtime.iterdir()) == []


@pytest.mark.asyncio
async def test_remote_watcher_cleans_runtime_after_supervisor_is_killed(
    command_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _inspector, _master, _paths = command_stack
    runtime = tmp_path / "remote-runtime"
    runtime.mkdir()
    monkeypatch.setenv("TMPDIR", str(runtime))
    task = asyncio.create_task(runner.execute("exec sleep 10"))
    async with asyncio.timeout(2):
        while not list(runtime.iterdir()):
            await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(3):
        while list(runtime.iterdir()):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_cancellation_during_runtime_creation_cleans_runtime(
    command_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _inspector, _master, _paths = command_stack
    runtime = tmp_path / "remote-runtime"
    runtime.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_mkdir = shutil.which("mkdir")
    assert real_mkdir is not None
    fake_mkdir = fake_bin / "mkdir"
    fake_mkdir.write_text(
        f'#!/bin/sh\nset -eu\n{shlex.quote(real_mkdir)} "$@"\nsleep 0.2\n',
        encoding="utf-8",
    )
    fake_mkdir.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", str(runtime))
    task = asyncio.create_task(runner.execute("exec sleep 10"))
    async with asyncio.timeout(2):
        while not list(runtime.iterdir()):
            await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(runtime.iterdir()) == []


@pytest.mark.asyncio
async def test_payload_copy_failure_does_not_orphan_fifo_reader(
    command_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _inspector, _master, _paths = command_stack
    runtime = tmp_path / "remote-runtime"
    runtime.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dd = fake_bin / "dd"
    fake_dd.write_text("#!/bin/sh\nexit 74\n", encoding="utf-8")
    fake_dd.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", str(runtime))

    async with asyncio.timeout(2):
        result = await runner.execute("exec sleep 10")

    assert result.exit_code == 74
    assert list(runtime.iterdir()) == []


@pytest.mark.asyncio
async def test_watcher_is_armed_before_payload_releases_child(
    command_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _inspector, _master, _paths = command_stack
    runtime = tmp_path / "remote-runtime"
    runtime.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_dd = shutil.which("dd")
    assert real_dd is not None
    fake_dd = fake_bin / "dd"
    fake_dd.write_text(
        f"""#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

parent = os.getppid()
deadline = time.monotonic() + 1
while time.monotonic() < deadline:
    descendants = {{parent}}
    statuses = []
    for status_path in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(
                line.split(":", 1)
                for line in status_path.read_text(encoding="ascii").splitlines()
                if ":" in line
            )
        except OSError:
            continue
        statuses.append((int(fields["Pid"].strip()), int(fields["PPid"].strip())))
    changed = True
    while changed:
        changed = False
        for process_id, parent_id in statuses:
            if parent_id in descendants and process_id not in descendants:
                descendants.add(process_id)
                changed = True
    for process_id in descendants - {{parent}}:
        try:
            command = Path(f"/proc/{{process_id}}/cmdline").read_bytes()
        except OSError:
            continue
        if b"remote-ssh-mcp-watcher" in command:
            os.execv({real_dd!r}, [{real_dd!r}, *sys.argv[1:]])
    time.sleep(0.01)
raise SystemExit(75)
""",
        encoding="utf-8",
    )
    fake_dd.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", str(runtime))

    result = await runner.execute("printf ready")

    assert result.exit_code == 0
    assert not result.timed_out
    assert result.stdout.raw == b"ready"
    assert list(runtime.iterdir()) == []


@pytest.mark.asyncio
async def test_watcher_detaches_only_after_registration_and_arming(
    command_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _inspector, _master, _paths = command_stack
    runtime = tmp_path / "remote-runtime"
    runtime.mkdir()
    marker = tmp_path / "watcher-detached-too-early"
    checked = tmp_path / "watcher-detach-checked"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_setsid = shutil.which("setsid")
    assert real_setsid is not None
    fake_setsid = fake_bin / "setsid"
    fake_setsid.write_text(
        f"""#!/bin/sh
set -eu
if test "${{4-}}" = remote-ssh-mcp-watcher; then
    if test -e "$7" || test -e "$8"; then
        : > "$REMOTE_SSH_MCP_TEST_MARKER"
    fi
    : > "$REMOTE_SSH_MCP_TEST_CHECKED"
fi
exec {shlex.quote(real_setsid)} "$@"
""",
        encoding="utf-8",
    )
    fake_setsid.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", str(runtime))
    monkeypatch.setenv("REMOTE_SSH_MCP_TEST_MARKER", str(marker))
    monkeypatch.setenv("REMOTE_SSH_MCP_TEST_CHECKED", str(checked))

    result = await runner.execute(
        f"while test ! -e {shlex.quote(str(checked))}; do sleep 0.01; done\n"
        "printf ready",
        timeout=10,
    )

    assert result.exit_code == 0
    assert result.stdout.raw == b"ready"
    assert checked.exists()
    assert not marker.exists()
    assert list(runtime.iterdir()) == []


@pytest.mark.asyncio
async def test_close_terminates_active_commands_and_rejects_new_ones(
    command_stack, tmp_path: Path
) -> None:
    runner, _inspector, _master, _paths = command_stack
    pid_file = tmp_path / "disconnect-command.pid"
    task = asyncio.create_task(
        runner.execute(f"echo $$ > {shlex.quote(str(pid_file))}; exec sleep 10")
    )
    async with asyncio.timeout(2):
        while not pid_file.exists():
            await asyncio.sleep(0.01)
    pid = int(pid_file.read_text(encoding="utf-8"))

    await runner.close()
    with pytest.raises(RemoteMCPError) as active:
        await task
    assert active.value.code == "connection_lost"
    with pytest.raises(RemoteMCPError) as new:
        await runner.execute("true")
    assert new.value.code == "connection_lost"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_invalid_commands_are_rejected_before_master_check(command_stack) -> None:
    runner, _inspector, master, _paths = command_stack

    with pytest.raises(RemoteMCPError, match="non-empty"):
        await runner.execute("")
    with pytest.raises(RemoteMCPError, match="NUL"):
        await runner.execute("echo\x00bad")

    assert master.ready_checks == 0


@pytest.mark.asyncio
async def test_stat_and_range_read(command_stack, tmp_path: Path) -> None:
    _runner, inspector, _master, _paths = command_stack
    remote_file = tmp_path / "remote data.bin"
    remote_file.write_bytes(b"0123456789")

    metadata = await inspector.stat(str(remote_file))
    data = await inspector.read_file_range(str(remote_file), offset=3, max_bytes=4)

    assert metadata["size"] == 10
    assert metadata["type"] == "regular file"
    assert data["data"] == "3456"
    assert data["bytes_read"] == 4
    assert not data["eof"]

    final = await inspector.read_file_range(str(remote_file), offset=6, max_bytes=4)
    assert final["data"] == "6789"
    assert final["bytes_read"] == 4
    assert final["eof"]


@pytest.mark.asyncio
async def test_directory_listing_preserves_unusual_names(
    command_stack, tmp_path: Path
) -> None:
    _runner, inspector, _master, _paths = command_stack
    directory = tmp_path / "listing"
    directory.mkdir()
    (directory / "line\nbreak.txt").write_text("x", encoding="utf-8")
    (directory / "plain.txt").write_text("yz", encoding="utf-8")

    result = await inspector.list_directory(str(directory))

    names = {entry["name"]["data"] for entry in result["entries"]}
    assert names == {"line\nbreak.txt", "plain.txt"}
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_directory_listing_rejects_non_directory_source(
    command_stack, tmp_path: Path
) -> None:
    _runner, inspector, _master, _paths = command_stack
    remote_file = tmp_path / "not-a-directory"
    remote_file.write_text("data", encoding="utf-8")

    with pytest.raises(RemoteMCPError) as raised:
        await inspector.list_directory(str(remote_file))

    assert raised.value.code == "invalid_remote_type"


@pytest.mark.asyncio
async def test_missing_remote_path_has_stable_error(command_stack) -> None:
    _runner, inspector, _master, _paths = command_stack

    with pytest.raises(RemoteMCPError) as raised:
        await inspector.stat("/definitely/missing/remote-ssh-mcp")

    assert raised.value.code == "remote_path_not_found"


@pytest.mark.asyncio
async def test_range_limits_are_enforced(command_stack) -> None:
    runner, inspector, _master, _paths = command_stack

    with pytest.raises(RemoteMCPError) as raised:
        await inspector.read_file_range(
            "file", max_bytes=runner.config.max_output_bytes + 1
        )

    assert raised.value.code == "invalid_range"


@pytest.mark.asyncio
async def test_range_read_rejects_non_regular_source(
    command_stack, tmp_path: Path
) -> None:
    _runner, inspector, _master, _paths = command_stack
    directory = tmp_path / "directory"
    directory.mkdir()

    with pytest.raises(RemoteMCPError) as raised:
        await inspector.read_file_range(str(directory))

    assert raised.value.code == "invalid_remote_type"
