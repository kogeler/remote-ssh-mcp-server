#!/usr/bin/env python3
"""Shared live black-box test for remote-ssh-mcp against a Podman target.

This file is intentionally not named ``test_*.py``. Run it only against a
disposable container prepared for this test and with the required environment
variables set.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import CallToolResult
from ssh_wrapper.session_environment import SESSION_ENVIRONMENT_VARIABLES

REMOTE_ROOT = "/srv/remote-ssh-mcp-e2e"
FINAL_STATES = {"completed", "failed", "cancelled"}


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


CONTAINER = required_environment("REMOTE_SSH_MCP_E2E_CONTAINER")
TARGET = required_environment("REMOTE_SSH_MCP_E2E_TARGET")
REPOSITORY_ROOT = Path(required_environment("REMOTE_SSH_MCP_E2E_REPOSITORY"))
LAUNCHER = Path(
    os.environ.get("REMOTE_SSH_MCP_E2E_LAUNCHER", REPOSITORY_ROOT / "remote-ssh-mcp")
)
PODMAN = os.environ.get("PODMAN", "podman")

# The server under test runs either on this host, next to a hardware key, or in
# a second container on a private network with the target. SERVER_CONTAINER
# selects which, and every access to the server's repository goes through
# the helpers below so the matrix itself does not care.
SERVER_CONTAINER = os.environ.get("REMOTE_SSH_MCP_E2E_SERVER_CONTAINER", "")
SSH_CONFIG = os.environ.get("REMOTE_SSH_MCP_TEST_SSH_CONFIG", "")
WRAPPER_DIR = os.environ.get("REMOTE_SSH_MCP_E2E_WRAPPER_DIR", "")
STRIP_SESSION_ENVIRONMENT = (
    os.environ.get("REMOTE_SSH_MCP_E2E_STRIP_SESSION_ENVIRONMENT") == "1"
)


def evidence(message: str) -> None:
    print(f"E2E_PASS {message}", flush=True)


def run_target(*arguments: str, input_data: bytes | None = None) -> bytes:
    """Run an administrative command inside the disposable target container."""
    completed = subprocess.run(
        [PODMAN, "exec", CONTAINER, *arguments],
        input=input_data,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"target command {arguments[0]!r} failed with status {completed.returncode}"
        )
    return completed.stdout


def target_log() -> str:
    """Return the container log, which is the only sshd authentication record."""
    completed = subprocess.run(
        [PODMAN, "logs", CONTAINER],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"reading the target log failed with status {completed.returncode}"
        )
    return (completed.stdout + completed.stderr).decode("utf-8", errors="replace")


def target_interface() -> str:
    """Return the container's outbound interface.

    Rootless Podman names it after the host template interface rather than
    always using eth0, so it has to be discovered.
    """
    if SERVER_CONTAINER:
        inspected = subprocess.run(
            [
                PODMAN,
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                SERVER_CONTAINER,
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        assert inspected.returncode == 0
        server_address = inspected.stdout.strip()
        assert server_address
        route = run_target("ip", "route", "get", server_address).decode()
    else:
        route = run_target("ip", "route", "show", "default").decode()
    fields = route.split()
    if "dev" not in fields:
        raise AssertionError("the target has no default route")
    return fields[fields.index("dev") + 1]


def local_run(*arguments: str, input_data: bytes | None = None) -> bytes:
    """Run a command on whichever side owns the server repository."""
    command = list(arguments)
    if SERVER_CONTAINER:
        command = [PODMAN, "exec", "--interactive", SERVER_CONTAINER, *arguments]
    completed = subprocess.run(
        command,
        input=input_data,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"local command {arguments[0]!r} failed with status "
            f"{completed.returncode}: {completed.stderr.decode(errors='replace')}"
        )
    return completed.stdout


def local_sha256(path: Path) -> str:
    if SERVER_CONTAINER:
        return local_run("sha256sum", str(path)).decode().split()[0]
    return hash_file(path)


def local_read(path: Path) -> bytes:
    if SERVER_CONTAINER:
        return local_run("cat", str(path))
    return path.read_bytes()


def local_write(path: Path, content: bytes) -> None:
    if SERVER_CONTAINER:
        local_run(
            "sh",
            "-c",
            'umask 077; cat > "$1"',
            "remote-ssh-mcp-write",
            str(path),
            input_data=content,
        )
        return
    path.write_bytes(content)


def local_symlink(path: Path, destination: str) -> None:
    if SERVER_CONTAINER:
        local_run("ln", "-s", destination, str(path))
        return
    path.symlink_to(destination)


def local_largest_partial(directory: Path) -> int:
    """Return the size of the largest resumable partial, or 0 if there is none."""
    if SERVER_CONTAINER:
        listing = local_run(
            "sh",
            "-c",
            f"find {directory} -name '*.download' -printf '%s\\n' 2>/dev/null | "
            "sort -n | tail -n 1",
        )
        text = listing.decode().strip()
        return int(text) if text else 0
    sizes = [path.stat().st_size for path in directory.glob("*.download")]
    return max(sizes, default=0)


def provide_upload_source(path: Path, marker: bytes, size: int) -> str:
    """Create the upload payload on the side that runs the server."""
    if not SERVER_CONTAINER:
        return write_large_local_file(path, marker, size)
    with tempfile.TemporaryDirectory() as staging:
        staged = Path(staging) / path.name
        digest = write_large_local_file(staged, marker, size)
        local_run(
            "sh",
            "-c",
            'umask 077; cat > "$1"',
            "remote-ssh-mcp-upload",
            str(path),
            input_data=staged.read_bytes(),
        )
    return digest


def install_sudo_policy(mode: str) -> None:
    rules = {
        "nopasswd": ("mcp-test ALL=(root) NOPASSWD: /bin/bash --noprofile --norc -s"),
        "password": "mcp-test ALL=(root) /bin/bash --noprofile --norc -s",
        "denied": "mcp-test ALL=(root) NOPASSWD: /usr/bin/id",
    }
    rule = rules[mode]
    if mode == "denied":
        defaults = "Defaults:mcp-test !authenticate"
    else:
        defaults = "Defaults:mcp-test timestamp_type=global,timestamp_timeout=5"
    script = (
        "set -e; "
        f"printf '%s\\n' {shlex.quote(defaults)} {shlex.quote(rule)} "
        "> /etc/sudoers.d/99-remote-ssh-mcp-e2e; "
        "chmod 0440 /etc/sudoers.d/99-remote-ssh-mcp-e2e; "
        "visudo -cf /etc/sudoers.d/99-remote-ssh-mcp-e2e >/dev/null"
    )
    run_target("/bin/bash", "-c", script)


def seed_global_sudo_timestamp() -> None:
    run_target("runuser", "-u", "mcp-test", "--", "sudo", "-n", "-v")


def set_download_rate_limit(enabled: bool) -> None:
    interface = target_interface()
    if enabled:
        run_target(
            "tc",
            "qdisc",
            "replace",
            "dev",
            interface,
            "root",
            "tbf",
            "rate",
            "4mbit",
            "burst",
            "64kb",
            "latency",
            "500ms",
        )
        return
    completed = subprocess.run(
        [PODMAN, "exec", CONTAINER, "tc", "qdisc", "del", "dev", interface, "root"],
        capture_output=True,
        check=False,
    )
    if completed.returncode not in {0, 2}:
        raise AssertionError(
            f"removing the target rate limit failed with status {completed.returncode}"
        )


def structured(result: CallToolResult) -> dict[str, Any]:
    payload = result.structured_content
    assert isinstance(payload, dict)
    return payload


async def call_ok(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await session.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    payload = structured(result)
    assert not result.is_error, (name, payload)
    assert payload["ok"] is True
    value = payload["result"]
    assert isinstance(value, dict)
    return value


async def call_ok_list(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> list[dict[str, Any]]:
    result = await session.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    payload = structured(result)
    assert not result.is_error, (name, payload)
    assert payload["ok"] is True
    value = payload["result"]
    assert isinstance(value, list)
    return value


async def call_error(
    session: ClientSession,
    name: str,
    arguments: dict[str, Any],
    expected_code: str,
) -> dict[str, Any]:
    result = await session.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    payload = structured(result)
    assert result.is_error, (name, payload)
    assert payload["ok"] is False
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == expected_code, (expected_code, error)
    assert set(error) == {"code", "message"}
    return error


async def start_transfer(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    operation = await call_ok(session, name, arguments)
    operation_id = operation["operation_id"]
    assert isinstance(operation_id, str)
    assert len(operation_id) == 32
    return operation_id, operation


async def wait_transfer(
    session: ClientSession, operation_id: str, timeout: float = 180
) -> dict[str, Any]:
    async with asyncio.timeout(timeout):
        while True:
            operation = await call_ok(
                session, "transfer_status", {"operation_id": operation_id}
            )
            if operation["state"] in FINAL_STATES:
                return operation
            await asyncio.sleep(0.1)


async def wait_for_partial(
    session: ClientSession, operation_id: str, timeout: float = 30
) -> dict[str, Any]:
    async with asyncio.timeout(timeout):
        while True:
            operation = await call_ok(
                session, "transfer_status", {"operation_id": operation_id}
            )
            if int(operation["bytes_transferred"]) > 0:
                return operation
            await asyncio.sleep(0.1)


def write_large_local_file(path: Path, marker: bytes, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    chunk = (marker + bytes(range(256))) * 4096
    with path.open("wb") as output:
        while remaining:
            piece = chunk[: min(remaining, len(chunk))]
            output.write(piece)
            digest.update(piece)
            remaining -= len(piece)
    return digest.hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def exercise_commands(session: ClientSession) -> None:
    stdout = await call_ok(session, "exec", {"command": "printf stdout-only"})
    assert stdout["stdout"]["data"] == "stdout-only"
    assert stdout["stderr"]["data"] == ""

    stderr = await call_ok(session, "exec", {"command": "printf stderr-only >&2"})
    assert stderr["stdout"]["data"] == ""
    assert stderr["stderr"]["data"] == "stderr-only"

    mixed = await call_ok(
        session,
        "exec",
        {"command": "printf mixed-out; printf mixed-err >&2; exit 17"},
    )
    assert mixed["exit_code"] == 17
    assert mixed["stdout"]["data"] == "mixed-out"
    assert mixed["stderr"]["data"] == "mixed-err"

    empty = await call_ok(session, "exec", {"command": ":"})
    assert empty["stdout"]["total_bytes"] == 0
    assert empty["stderr"]["total_bytes"] == 0

    working = await call_ok(session, "exec", {"command": "pwd", "cwd": REMOTE_ROOT})
    assert working["stdout"]["data"].strip() == REMOTE_ROOT

    await call_ok(session, "exec", {"command": "export RSM_E2E_LEAK=value"})
    isolated = await call_ok(
        session, "exec", {"command": 'test -z "${RSM_E2E_LEAK+x}"'}
    )
    assert isolated["exit_code"] == 0

    timed = await call_ok(session, "exec", {"command": "sleep 5", "timeout": 0.3})
    assert timed["timed_out"] is True

    binary = await call_ok(
        session,
        "exec",
        {"command": "printf '\\377\\376binary\\000tail'"},
    )
    assert binary["stdout"]["encoding"] == "base64"
    assert base64.b64decode(binary["stdout"]["data"]) == b"\xff\xfebinary\x00tail"

    bounded = await call_ok(
        session,
        "exec",
        {
            "command": "head -c 16384 /dev/zero | tr '\\000' x",
            "spool_output": True,
        },
    )
    assert bounded["stdout"]["captured_bytes"] == 4096
    assert bounded["stdout"]["total_bytes"] == 16384
    assert bounded["stdout"]["truncated"] is True
    assert bounded["stdout"]["spool_path"].startswith(".remote-ssh-mcp/spool/")

    long_call = asyncio.create_task(
        session.call_tool(
            "exec",
            {
                "command": (
                    f"echo $$ > {REMOTE_ROOT}/cancelled-command.pid; exec sleep 30"
                )
            },
        )
    )
    await asyncio.sleep(1)
    long_call.cancel()
    try:
        await long_call
    except asyncio.CancelledError:
        pass
    async with asyncio.timeout(5):
        while True:
            stopped = await call_ok(
                session,
                "exec",
                {
                    "command": (
                        f"pid=$(cat {REMOTE_ROOT}/cancelled-command.pid); "
                        '! kill -0 "$pid" 2>/dev/null'
                    )
                },
            )
            if stopped["exit_code"] == 0:
                break
            await asyncio.sleep(0.1)
    evidence("exec matrix, timeout, output bounds, and cancellation")


async def exercise_inspection(session: ClientSession) -> None:
    metadata = await call_ok(
        session, "stat", {"remote_path": f"{REMOTE_ROOT}/normal.txt"}
    )
    assert metadata["type"] == "regular file"
    assert metadata["size"] == len(b"normal-data")

    listing = await call_ok(session, "list_directory", {"remote_path": REMOTE_ROOT})
    names = {
        entry["name"]["data"]
        for entry in listing["entries"]
        if entry["name"]["encoding"] == "utf-8"
    }
    assert 'space "quote" semi; unicode.txt' in names

    ranged = await call_ok(
        session,
        "read_file_range",
        {
            "remote_path": f"{REMOTE_ROOT}/normal.txt",
            "offset": 2,
            "max_bytes": 4,
        },
    )
    assert ranged["data"] == "rmal"
    assert ranged["eof"] is False

    encoded = await call_ok(
        session,
        "read_file_range",
        {"remote_path": f"{REMOTE_ROOT}/binary.bin", "max_bytes": 64},
    )
    assert encoded["encoding"] == "base64"
    assert base64.b64decode(encoded["data"]) == b"\xff\xfebinary\x00tail"

    await call_error(
        session,
        "stat",
        {"remote_path": f"{REMOTE_ROOT}/missing"},
        "remote_path_not_found",
    )
    await call_error(
        session,
        "list_directory",
        {"remote_path": f"{REMOTE_ROOT}/denied-dir"},
        "remote_path_not_found",
    )
    await call_error(
        session,
        "read_file_range",
        {"remote_path": f"{REMOTE_ROOT}/denied.txt"},
        "remote_path_not_found",
    )
    await call_error(
        session,
        "read_file_range",
        {"remote_path": f"{REMOTE_ROOT}/normal.txt", "max_bytes": 4097},
        "invalid_range",
    )
    evidence("stat, directory listing, ranges, unusual names, and failures")


async def exercise_sudo(session: ClientSession) -> None:
    privileged = await call_ok(
        session,
        "sudo_exec",
        {"command": "test ! -t 0 && test ! -t 1 && id -u"},
    )
    assert privileged["exit_code"] == 0
    assert privileged["stdout"]["data"].strip() == "0"

    failed_command = await call_ok(session, "sudo_exec", {"command": "exit 23"})
    assert failed_command["exit_code"] == 23

    install_sudo_policy("password")
    await call_error(
        session, "sudo_exec", {"command": "id -u"}, "sudo_password_required"
    )
    install_sudo_policy("nopasswd")
    seed_global_sudo_timestamp()
    install_sudo_policy("password")
    await call_error(
        session, "sudo_exec", {"command": "id -u"}, "sudo_password_required"
    )

    install_sudo_policy("denied")
    await call_error(session, "sudo_exec", {"command": "id -u"}, "sudo_not_allowed")
    install_sudo_policy("nopasswd")
    evidence("NOPASSWD, nonzero command, password/cache refusal, and policy denial")


async def exercise_transfers(session: ClientSession, marker: bytes) -> None:
    download_id, download_start = await start_transfer(
        session,
        "download_start",
        {
            "remote_path": f"{REMOTE_ROOT}/large-download.bin",
            "local_path": "downloads/large-download.bin",
        },
    )
    assert "data" not in repr(download_start)
    download = await wait_transfer(session, download_id)
    assert download["state"] == "completed"
    local_download = REPOSITORY_ROOT / "downloads/large-download.bin"
    assert download["sha256"] == local_sha256(local_download)
    assert marker.decode() not in repr(download)

    upload_source = REPOSITORY_ROOT / "upload-large.bin"
    upload_hash = provide_upload_source(upload_source, marker, 16 * 1024 * 1024)
    upload_id, _ = await start_transfer(
        session,
        "upload_start",
        {
            "local_path": upload_source.name,
            "remote_path": f"{REMOTE_ROOT}/uploaded-large.bin",
        },
    )
    upload = await wait_transfer(session, upload_id)
    assert upload["state"] == "completed"
    assert upload["sha256"] == upload_hash
    remote_hash = await call_ok(
        session,
        "exec",
        {"command": f"sha256sum -- {REMOTE_ROOT}/uploaded-large.bin | cut -d' ' -f1"},
    )
    assert remote_hash["stdout"]["data"].strip() == upload_hash

    refused_id, _ = await start_transfer(
        session,
        "upload_start",
        {
            "local_path": upload_source.name,
            "remote_path": f"{REMOTE_ROOT}/uploaded-large.bin",
        },
    )
    refused = await wait_transfer(session, refused_id)
    assert refused["state"] == "failed"
    assert refused["error"]["code"] == "remote_path_exists"

    local_write(upload_source, b"explicit-overwrite")
    overwrite_hash = hashlib.sha256(b"explicit-overwrite").hexdigest()
    overwrite_id, _ = await start_transfer(
        session,
        "upload_start",
        {
            "local_path": upload_source.name,
            "remote_path": f"{REMOTE_ROOT}/uploaded-large.bin",
            "overwrite": True,
        },
    )
    overwritten = await wait_transfer(session, overwrite_id)
    assert overwritten["state"] == "completed"
    assert overwritten["sha256"] == overwrite_hash

    await call_error(
        session,
        "download_start",
        {
            "remote_path": f"{REMOTE_ROOT}/normal.txt",
            "local_path": "downloads/large-download.bin",
        },
        "local_path_exists",
    )
    replace_id, _ = await start_transfer(
        session,
        "download_start",
        {
            "remote_path": f"{REMOTE_ROOT}/normal.txt",
            "local_path": "downloads/large-download.bin",
            "overwrite": True,
        },
    )
    replaced = await wait_transfer(session, replace_id)
    assert replaced["state"] == "completed"
    assert local_read(local_download) == b"normal-data"

    missing_id, _ = await start_transfer(
        session,
        "download_start",
        {
            "remote_path": f"{REMOTE_ROOT}/missing-transfer",
            "local_path": "downloads/missing.bin",
        },
    )
    missing = await wait_transfer(session, missing_id)
    assert missing["state"] == "failed"
    assert missing["error"]["code"] == "remote_path_not_found"

    unusual_id, _ = await start_transfer(
        session,
        "download_start",
        {
            "remote_path": f'{REMOTE_ROOT}/space "quote" semi; unicode.txt',
            "local_path": "downloads/unusual.txt",
        },
    )
    unusual = await wait_transfer(session, unusual_id)
    assert unusual["state"] == "completed"
    assert local_read(REPOSITORY_ROOT / "downloads/unusual.txt") == b"unusual-data"

    await call_error(
        session,
        "download_start",
        {
            "remote_path": f"{REMOTE_ROOT}/normal.txt",
            "local_path": "../escape",
        },
        "invalid_local_path",
    )
    local_symlink(REPOSITORY_ROOT / "escape-link", "/tmp")
    await call_error(
        session,
        "download_start",
        {
            "remote_path": f"{REMOTE_ROOT}/normal.txt",
            "local_path": "escape-link/outside",
        },
        "invalid_local_path",
    )

    set_download_rate_limit(True)
    try:
        cancel_id, _ = await start_transfer(
            session,
            "download_start",
            {
                "remote_path": f"{REMOTE_ROOT}/cancel-download.bin",
                "local_path": "downloads/resumed.bin",
            },
        )
        partial = await wait_for_partial(session, cancel_id)
        assert int(partial["bytes_transferred"]) > 0
        cancelled = await call_ok(
            session, "transfer_cancel", {"operation_id": cancel_id}
        )
        assert cancelled["state"] == "cancelled"
        assert int(cancelled["bytes_transferred"]) > 0
    finally:
        set_download_rate_limit(False)

    partial_size = local_largest_partial(REPOSITORY_ROOT / ".remote-ssh-mcp/partials")
    assert partial_size > 0
    resume_id, _ = await start_transfer(
        session,
        "download_start",
        {
            "remote_path": f"{REMOTE_ROOT}/cancel-download.bin",
            "local_path": "downloads/resumed.bin",
        },
    )
    resumed = await wait_transfer(session, resume_id)
    assert resumed["state"] == "completed"
    assert resumed["total_bytes"] == 128 * 1024 * 1024
    assert resumed["sha256"] == local_sha256(REPOSITORY_ROOT / "downloads/resumed.bin")

    set_download_rate_limit(True)
    active_ids: list[str] = []
    try:
        first_id, _ = await start_transfer(
            session,
            "download_start",
            {
                "remote_path": f"{REMOTE_ROOT}/cancel-download.bin",
                "local_path": "downloads/limit-one.bin",
            },
        )
        active_ids.append(first_id)
        await call_error(
            session,
            "download_start",
            {
                "remote_path": f"{REMOTE_ROOT}/cancel-download.bin",
                "local_path": "downloads/limit-one.bin",
            },
            "transfer_conflict",
        )
        second_id, _ = await start_transfer(
            session,
            "download_start",
            {
                "remote_path": f"{REMOTE_ROOT}/cancel-download.bin",
                "local_path": "downloads/limit-two.bin",
            },
        )
        active_ids.append(second_id)
        await call_error(
            session,
            "download_start",
            {
                "remote_path": f"{REMOTE_ROOT}/cancel-download.bin",
                "local_path": "downloads/limit-three.bin",
            },
            "transfer_limit_reached",
        )
        for operation_id in active_ids:
            cancelled = await call_ok(
                session, "transfer_cancel", {"operation_id": operation_id}
            )
            assert cancelled["state"] == "cancelled"
    finally:
        set_download_rate_limit(False)

    transfers = await call_ok_list(session, "transfer_list", {})
    assert len(transfers) == 11
    await call_error(
        session,
        "transfer_status",
        {"operation_id": "0" * 32},
        "transfer_not_found",
    )
    evidence("download, upload, hashes, overwrite, cancel/resume, and limits")


async def prove_explicit_disconnect(
    session: ClientSession, master_pid: int, accepted_before: int
) -> None:
    established = run_target(
        "/bin/bash",
        "-c",
        "ss -Htn state established '( sport = :22 )' | wc -l",
    )
    assert int(established.strip()) == 1

    active = asyncio.create_task(
        session.call_tool("exec", {"command": "exec sleep 60"})
    )
    await asyncio.sleep(1)
    disconnected = await call_ok(session, "disconnect", {})
    assert disconnected["state"] == "disconnected"
    assert disconnected["master_pid"] is None
    result = await asyncio.wait_for(active, timeout=15)
    assert isinstance(result, CallToolResult)
    payload = structured(result)
    assert result.is_error
    assert payload["error"]["code"] == "connection_lost"

    await call_error(
        session,
        "stat",
        {"remote_path": f"{REMOTE_ROOT}/normal.txt"},
        "not_connected",
    )
    status = await call_ok(session, "connection_status", {})
    assert status["state"] == "disconnected"
    if SERVER_CONTAINER:
        survived = await asyncio.to_thread(
            subprocess.run,
            [PODMAN, "exec", SERVER_CONTAINER, "/bin/kill", "-0", str(master_pid)],
            capture_output=True,
            check=False,
        )
        assert survived.returncode != 0, "SSH master process survived disconnect"
    else:
        try:
            os.kill(master_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("SSH master process survived disconnect")
    await asyncio.sleep(1)
    logs = target_log()
    accepted_after = logs.count("Accepted publickey for mcp-test")
    assert accepted_after == accepted_before == 1
    established_after = run_target(
        "/bin/bash",
        "-c",
        "ss -Htn state established '( sport = :22 )' | wc -l",
    )
    assert int(established_after.strip()) == 0
    evidence("explicit disconnect stopped active work, master, and transport")


def server_parameters(environment: dict[str, str]) -> StdioServerParameters:
    """Describe how to start the server under test on the selected side."""
    options = [
        "--connect-timeout",
        "300",
        "--command-timeout",
        "10",
        "--max-output-bytes",
        "4096",
        "--max-transfers",
        "2",
        "--log-level",
        "INFO",
    ]
    if SERVER_CONTAINER:
        return StdioServerParameters(
            command=PODMAN,
            args=[
                "exec",
                "--interactive",
                "--workdir",
                str(REPOSITORY_ROOT),
                SERVER_CONTAINER,
                "/usr/local/bin/python",
                "/work/src/remote_ssh_mcp/remote-ssh-mcp.py",
                *options,
            ],
            env=environment,
        )
    return StdioServerParameters(
        command=str(LAUNCHER),
        args=options,
        env=environment,
        cwd=Path(SSH_CONFIG).parent,
    )


async def main() -> None:
    assert REPOSITORY_ROOT.is_absolute()
    marker = b"REMOTE_SSH_MCP_TRANSFER_PAYLOAD_SENTINEL_8d73374c"
    environment = os.environ.copy()
    if SERVER_CONTAINER:
        stderr_path = Path(required_environment("REMOTE_SSH_MCP_E2E_STDERR"))
    else:
        assert REPOSITORY_ROOT.is_dir()
        assert LAUNCHER.parent == REPOSITORY_ROOT
        assert Path(SSH_CONFIG).is_file()
        assert Path(WRAPPER_DIR, "ssh").is_file()
        stderr_path = Path(required_environment("REMOTE_SSH_MCP_E2E_STDERR"))
        environment["PATH"] = f"{WRAPPER_DIR}:{environment['PATH']}"
        environment["REMOTE_SSH_MCP_TEST_SSH_CONFIG"] = SSH_CONFIG
    if STRIP_SESSION_ENVIRONMENT:
        assert not SERVER_CONTAINER
        removed = {
            name: environment.pop(name, None) for name in SESSION_ENVIRONMENT_VARIABLES
        }
        assert any(removed.values()), (
            "no active session environment was available to strip"
        )
        assert all(name not in environment for name in SESSION_ENVIRONMENT_VARIABLES)
        evidence("MCP subprocess session environment was deliberately sanitized")
    parameters = server_parameters(environment)

    with stderr_path.open(  # noqa: ASYNC230 - opened before process supervision.
        "w+", encoding="utf-8"
    ) as errlog:
        async with (
            stdio_client(parameters, errlog=errlog) as (read, write),
            ClientSession(read, write, read_timeout_seconds=360) as session,
        ):
            initialized = await session.initialize()
            assert initialized.server_info.name == "remote-ssh-mcp"
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "connect",
                "disconnect",
                "connection_status",
                "exec",
                "sudo_exec",
                "stat",
                "list_directory",
                "read_file_range",
                "download_start",
                "upload_start",
                "transfer_status",
                "transfer_cancel",
                "transfer_list",
            }
            assert all(
                tool.input_schema.get("additionalProperties") is False
                for tool in tools.tools
            )
            evidence("MCP initialize, capabilities, tool list, and strict schemas")

            status = await call_ok(session, "connection_status", {})
            assert status["state"] == "disconnected"
            await call_error(
                session,
                "exec",
                {"command": "true"},
                "not_connected",
            )
            evidence("MCP startup remained disconnected and did not invoke SSH")

            connected = await call_ok(session, "connect", {"ssh_alias": TARGET})
            assert connected["state"] == "ready"
            assert connected["mode"] == "alias"
            assert connected["ssh_alias"] == TARGET
            await call_error(
                session,
                "connect",
                {"ssh_alias": TARGET},
                "already_connected",
            )

            status = await call_ok(session, "connection_status", {})
            assert status["state"] == "ready"
            assert status["target"] == TARGET
            master_pid = status["master_pid"]
            assert isinstance(master_pid, int)
            evidence("single owned OpenSSH master ready")

            await call_error(
                session,
                "stat",
                {"remote_path": f"{REMOTE_ROOT}/normal.txt", "target": "other"},
                "invalid_arguments",
            )
            await exercise_commands(session)
            await exercise_inspection(session)
            await exercise_sudo(session)
            await exercise_transfers(session, marker)

            status_after = await call_ok(session, "connection_status", {})
            assert status_after["state"] == "ready"
            assert status_after["master_pid"] == master_pid
            logs = target_log()
            accepted = logs.count("Accepted publickey for mcp-test")
            established = run_target(
                "/bin/bash",
                "-c",
                "ss -Htn state established '( sport = :22 )' | wc -l",
            )
            assert accepted == 1
            assert int(established.strip()) == 1
            evidence("one authentication, one transport, and one stable master PID")
            await prove_explicit_disconnect(session, master_pid, accepted)

        errlog.flush()
        errlog.seek(0)
        diagnostics = errlog.read()

    assert '"jsonrpc"' not in diagnostics
    assert marker.decode() not in diagnostics
    assert "PRIVATE KEY" not in diagnostics
    evidence("stderr contains no protocol, private key, or payload marker")
    print("E2E_COMPLETE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
