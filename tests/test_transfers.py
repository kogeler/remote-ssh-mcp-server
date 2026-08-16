from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from remote_ssh_mcp.commands import CommandRunner
from remote_ssh_mcp.config import ConnectionSpec, RuntimeConfig
from remote_ssh_mcp.errors import RemoteMCPError
from remote_ssh_mcp.inspection import RemoteInspector
from remote_ssh_mcp.local_paths import LocalPathPolicy
from remote_ssh_mcp.transfers import TransferManager, TransferOperation

FAKE_RSYNC = r"""#!__PYTHON__
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
source_arg, destination_arg = args[-2:]
target = os.environ.get("FAKE_RSYNC_TARGET", "test-target") + ":"

def local_path(value):
    return Path(value[len(target):] if value.startswith(target) else value)

source = local_path(source_arg)
destination = local_path(destination_arg)
initial_size = destination.stat().st_size if destination.exists() else 0
log = os.environ.get("FAKE_RSYNC_LOG")
if log:
    with Path(log).open("a", encoding="utf-8") as output:
        output.write(json.dumps({"args": args, "initial_size": initial_size}) + "\n")

if os.environ.get("FAKE_RSYNC_FAIL") == "1":
    print("simulated rsync failure", file=sys.stderr)
    raise SystemExit(23)

source_size = source.stat().st_size
if initial_size > source_size:
    destination.unlink()
    initial_size = 0

delay = float(os.environ.get("FAKE_RSYNC_DELAY", "0"))
with source.open("rb") as incoming:
    incoming.seek(initial_size)
    mode = "ab" if initial_size else "wb"
    with destination.open(mode, buffering=0) as outgoing:
        copied = initial_size
        while data := incoming.read(65536):
            outgoing.write(data)
            copied += len(data)
            percent = 100 if source_size == 0 else copied * 100 // source_size
            print(f"\r{copied:,} {percent}% 1.00MB/s 0:00:00", end="", flush=True)
            if delay:
                time.sleep(delay)

if os.environ.get("FAKE_RSYNC_CORRUPT") == "1" and destination.stat().st_size:
    with destination.open("r+b") as output:
        first = output.read(1)
        output.seek(0)
        output.write(bytes([first[0] ^ 0xff]))
raise SystemExit(0)
"""


class LocalTransferMaster:
    def __init__(self) -> None:
        self.ready_checks = 0
        self.fail_after: int | None = None
        self.connection = ConnectionSpec.from_alias("test-target")

    async def ensure_ready(self) -> None:
        self.ready_checks += 1
        if self.fail_after is not None and self.ready_checks > self.fail_after:
            raise RemoteMCPError("connection_lost", "fake master is gone")

    def command_argv(self, remote_program: str) -> list[str]:
        return shlex.split(remote_program)

    def rsync_ssh_command(self) -> str:
        return "ssh -T -S /owned/mux -o ProxyCommand=/bin/false"


@pytest.fixture
def transfer_stack(
    runtime_config: RuntimeConfig, tmp_path: Path
) -> tuple[TransferManager, LocalTransferMaster, LocalPathPolicy, Path]:
    fake_rsync = tmp_path / "fake-rsync"
    fake_rsync.write_text(
        FAKE_RSYNC.replace("__PYTHON__", sys.executable), encoding="utf-8"
    )
    fake_rsync.chmod(0o755)
    config = replace(runtime_config, rsync_path=fake_rsync)
    paths = LocalPathPolicy(config.local_root)
    paths.initialize()
    master = LocalTransferMaster()
    runner = CommandRunner(config, master, paths)  # type: ignore[arg-type]
    inspector = RemoteInspector(runner)
    manager = TransferManager(
        config,
        master,
        paths,
        runner,
        inspector,  # type: ignore[arg-type]
    )
    return manager, master, paths, fake_rsync


async def wait_for_final(
    manager: TransferManager, operation_id: str, timeout: float = 5.0
) -> dict[str, object]:
    async with asyncio.timeout(timeout):
        while True:
            status = await manager.status(operation_id)
            if status["state"] in {"completed", "failed", "cancelled"}:
                return status
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_large_download_is_verified_and_atomically_published(
    transfer_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "remote source.bin"
    payload = b"0123456789abcdef" * 10_000
    source.write_bytes(payload)
    log = tmp_path / "rsync.log"
    monkeypatch.setenv("FAKE_RSYNC_LOG", str(log))

    started = await manager.start_download(str(source), "downloaded.bin")
    result = await wait_for_final(manager, str(started["operation_id"]))

    assert result["state"] == "completed"
    assert len(str(result["operation_id"])) == 32
    assert result["bytes_transferred"] == len(payload)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert (tmp_path / "downloaded.bin").read_bytes() == payload
    assert "data" not in result
    invocation = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert "--append-verify" in invocation["args"]
    assert "--protect-args" in invocation["args"]
    assert any("ProxyCommand=/bin/false" in value for value in invocation["args"])


@pytest.mark.asyncio
async def test_upload_is_verified_then_renamed(transfer_stack, tmp_path: Path) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "upload source.bin"
    destination = tmp_path / "remote destination.bin"
    payload = b"upload-payload" * 1000
    source.write_bytes(payload)

    started = await manager.start_upload(
        "upload source.bin", str(destination), overwrite=False
    )
    result = await wait_for_final(manager, str(started["operation_id"]))

    assert result["state"] == "completed"
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert destination.read_bytes() == payload
    assert not list(tmp_path.glob("remote destination.bin.remote-ssh-mcp-*.partial"))


@pytest.mark.asyncio
async def test_cancelled_download_resumes_the_deterministic_partial(
    transfer_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "large remote.bin"
    payload = bytes(range(256)) * 8192
    source.write_bytes(payload)
    log = tmp_path / "resume.log"
    monkeypatch.setenv("FAKE_RSYNC_LOG", str(log))
    monkeypatch.setenv("FAKE_RSYNC_DELAY", "0.01")

    first = await manager.start_download(str(source), "resumed.bin")
    first_id = str(first["operation_id"])
    async with asyncio.timeout(3):
        while (await manager.status(first_id))["bytes_transferred"] == 0:
            await asyncio.sleep(0.01)
    cancelled = await manager.cancel(first_id)
    assert cancelled["state"] == "cancelled"
    assert int(cancelled["bytes_transferred"]) > 0

    second = await manager.start_download(str(source), "resumed.bin")
    result = await wait_for_final(manager, str(second["operation_id"]))

    assert result["state"] == "completed"
    assert (tmp_path / "resumed.bin").read_bytes() == payload
    invocations = [json.loads(line) for line in log.read_text().splitlines()]
    assert invocations[1]["initial_size"] > 0


@pytest.mark.asyncio
async def test_failed_hash_leaves_existing_download_untouched(
    transfer_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _master, paths, _fake = transfer_stack
    source = tmp_path / "remote-hash-source"
    source.write_bytes(b"new data")
    final = tmp_path / "existing.bin"
    final.write_bytes(b"old data")
    monkeypatch.setenv("FAKE_RSYNC_CORRUPT", "1")

    started = await manager.start_download(str(source), "existing.bin", overwrite=True)
    result = await wait_for_final(manager, str(started["operation_id"]))

    assert result["state"] == "failed"
    assert result["error"]["error"] == "verification_failed"  # type: ignore[index]
    assert final.read_bytes() == b"old data"
    assert list((paths.internal_root / "partials").iterdir())


@pytest.mark.asyncio
async def test_unusual_names_remain_single_rsync_arguments(
    transfer_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "remote '$;\n Unicode file"
    source.write_bytes(b"safe")
    destination = "-local '$; Unicode file"
    log = tmp_path / "argv.log"
    sentinel = tmp_path / "INJECTED"
    monkeypatch.setenv("FAKE_RSYNC_LOG", str(log))

    started = await manager.start_download(f"{source}; touch {sentinel}", destination)
    result = await wait_for_final(manager, str(started["operation_id"]))

    assert result["state"] == "failed"
    assert not sentinel.exists()
    assert not log.exists()

    started = await manager.start_download(str(source), destination)
    result = await wait_for_final(manager, str(started["operation_id"]))
    invocation = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert result["state"] == "completed"
    assert invocation["args"][-2] == f"test-target:{source}"
    assert invocation["args"][-1].endswith(".download")


@pytest.mark.asyncio
async def test_existing_remote_upload_is_not_overwritten(
    transfer_stack, tmp_path: Path
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "local-upload"
    destination = tmp_path / "remote-existing"
    source.write_bytes(b"replacement")
    destination.write_bytes(b"original")

    started = await manager.start_upload("local-upload", str(destination))
    result = await wait_for_final(manager, str(started["operation_id"]))

    assert result["state"] == "failed"
    assert result["error"]["error"] == "remote_path_exists"  # type: ignore[index]
    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob("remote-existing.remote-ssh-mcp-*.partial"))


@pytest.mark.asyncio
async def test_upload_conflict_during_copy_removes_partial_before_failure(
    transfer_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "racing-upload"
    destination = tmp_path / "racing-destination"
    source.write_bytes(b"replacement")
    run_rsync = manager._run_rsync

    async def create_destination_then_copy(
        operation: TransferOperation, rsync_source: str, rsync_destination: str
    ) -> None:
        destination.write_bytes(b"concurrent data")
        await run_rsync(operation, rsync_source, rsync_destination)

    monkeypatch.setattr(manager, "_run_rsync", create_destination_then_copy)

    started = await manager.start_upload("racing-upload", str(destination))
    result = await wait_for_final(manager, str(started["operation_id"]))

    assert result["state"] == "failed"
    assert result["error"]["error"] == "remote_path_exists"  # type: ignore[index]
    assert destination.read_bytes() == b"concurrent data"
    assert not list(tmp_path.glob("racing-destination.remote-ssh-mcp-*.partial"))


@pytest.mark.asyncio
async def test_upload_overwrite_replaces_existing_regular_file(
    transfer_stack, tmp_path: Path
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "overwrite-source"
    destination = tmp_path / "overwrite-destination"
    source.write_bytes(b"replacement")
    destination.write_bytes(b"original")

    started = await manager.start_upload(
        "overwrite-source", str(destination), overwrite=True
    )
    result = await wait_for_final(manager, str(started["operation_id"]))

    assert result["state"] == "completed"
    assert destination.read_bytes() == b"replacement"


@pytest.mark.asyncio
async def test_remote_partial_symlink_is_rejected(
    transfer_stack, tmp_path: Path
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "partial-source"
    destination = tmp_path / "partial-destination"
    victim = tmp_path / "partial-victim"
    source.write_bytes(b"replacement")
    victim.write_bytes(b"protected")

    started = await manager.start_upload(
        "partial-source", str(destination), overwrite=True
    )
    operation = manager._operations[str(started["operation_id"])]
    assert operation._remote_partial is not None
    Path(operation._remote_partial).symlink_to(victim)
    result = await wait_for_final(manager, str(started["operation_id"]))

    assert result["state"] == "failed"
    assert result["error"]["error"] == "invalid_transfer_partial"  # type: ignore[index]
    assert victim.read_bytes() == b"protected"


@pytest.mark.asyncio
async def test_transfer_limit_and_destination_conflict_are_enforced(
    transfer_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "slow-source"
    source.write_bytes(b"x" * 2_000_000)
    monkeypatch.setenv("FAKE_RSYNC_DELAY", "0.02")

    first = await manager.start_download(str(source), "one.bin")
    with pytest.raises(RemoteMCPError) as conflict:
        await manager.start_download(str(source), "one.bin")
    assert conflict.value.code == "transfer_conflict"

    second = await manager.start_download(str(source), "two.bin")
    with pytest.raises(RemoteMCPError) as limited:
        await manager.start_download(str(source), "three.bin")
    assert limited.value.code == "transfer_limit_reached"

    await manager.cancel(str(first["operation_id"]))
    await manager.cancel(str(second["operation_id"]))


@pytest.mark.asyncio
async def test_lost_master_fails_without_starting_rsync(
    transfer_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, master, _paths, _fake = transfer_stack
    source = tmp_path / "remote"
    source.write_bytes(b"data")
    log = tmp_path / "never-rsync.log"
    monkeypatch.setenv("FAKE_RSYNC_LOG", str(log))
    master.fail_after = 1

    started = await manager.start_download(str(source), "lost.bin")
    result = await wait_for_final(manager, str(started["operation_id"]))

    assert result["state"] == "failed"
    assert result["error"]["error"] == "connection_lost"  # type: ignore[index]
    assert master.ready_checks == 2
    assert not log.exists()


@pytest.mark.asyncio
async def test_symlink_escape_is_rejected_before_master_check(
    transfer_stack, tmp_path: Path
) -> None:
    manager, master, _paths, _fake = transfer_stack
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RemoteMCPError) as raised:
        await manager.start_download("/remote/file", "escape/published")

    assert raised.value.code == "invalid_local_path"
    assert master.ready_checks == 0


@pytest.mark.asyncio
async def test_close_cancels_active_transfers(
    transfer_stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    source = tmp_path / "close-source"
    source.write_bytes(b"z" * 2_000_000)
    monkeypatch.setenv("FAKE_RSYNC_DELAY", "0.02")
    started = await manager.start_download(str(source), "closed.bin")

    await manager.close()
    result = await manager.status(str(started["operation_id"]))

    assert result["state"] == "cancelled"
    with pytest.raises(RemoteMCPError) as raised:
        await manager.start_download(str(source), "another.bin")
    assert raised.value.code == "transfer_manager_closed"


@pytest.mark.asyncio
async def test_completed_operation_metadata_expires(
    transfer_stack, tmp_path: Path
) -> None:
    manager, _master, _paths, _fake = transfer_stack
    manager.completed_ttl = 0.1
    source = tmp_path / "ttl-source"
    source.write_bytes(b"short")
    started = await manager.start_download(str(source), "ttl-result")
    result = await wait_for_final(manager, str(started["operation_id"]))
    assert result["state"] == "completed"

    await asyncio.sleep(0.12)

    assert await manager.list() == []
    with pytest.raises(RemoteMCPError) as raised:
        await manager.status(str(started["operation_id"]))
    assert raised.value.code == "transfer_not_found"
