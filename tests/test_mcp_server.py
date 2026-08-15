from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import tomllib
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from remote_ssh_mcp.config import RuntimeConfig
from remote_ssh_mcp.master import ConnectionState, OpenSSHMaster
from remote_ssh_mcp.mcp_models import ConnectInput
from remote_ssh_mcp.server import (
    SERVER_INSTRUCTIONS,
    TOOL_DEFINITIONS,
    RemoteSSHApplication,
)

FAKE_SSH = r"""#!__PYTHON__
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
socket_pid = Path(str(socket_path) + ".pid")

if "-O" in args:
    operation = value("-O")
    if operation == "check":
        raise SystemExit(0 if socket_path.exists() else 255)
    if operation == "exit":
        try:
            os.kill(int(socket_pid.read_text()), signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError):
            raise SystemExit(255)
        raise SystemExit(0)

if "-M" in args and "-N" in args:
    count_path = Path(os.environ["FAKE_SSH_AUTH_COUNT"])
    previous = int(count_path.read_text()) if count_path.exists() else 0
    count_path.write_text(str(previous + 1))
    Path(os.environ["FAKE_SSH_MASTER_PID"]).write_text(str(os.getpid()))
    Path(os.environ["FAKE_SSH_CONTROL_PATH"]).write_text(str(socket_path))
    if os.environ.get("FAKE_SSH_FAIL_MASTER") == "1":
        raise SystemExit(23)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(socket_path))
    server.listen()
    socket_pid.write_text(str(os.getpid()))
    stopping = False
    def stop(_signum, _frame):
        global stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        time.sleep(0.01)
    server.close()
    socket_path.unlink(missing_ok=True)
    socket_pid.unlink(missing_ok=True)
    raise SystemExit(0)

if not socket_path.exists():
    raise SystemExit(255)
separator = args.index("--")
remote_program = args[separator + 2]
os.execl("/bin/sh", "sh", "-c", remote_program)
"""


FAKE_SUDO = r"""#!__PYTHON__
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_SUDO_LOG"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps(args) + "\n")
if args[:3] != ["-n", "-k", "--"]:
    print("sudo: unsafe invocation", file=sys.stderr)
    raise SystemExit(2)
os.execv(args[3], args[3:])
"""


FAKE_RSYNC = r"""#!__PYTHON__
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_RSYNC_LOG"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps(args) + "\n")
source_arg, destination_arg = args[-2:]
prefix = "test-target:"

def local_path(value):
    return Path(value[len(prefix):] if value.startswith(prefix) else value)

source = local_path(source_arg)
destination = local_path(destination_arg)
initial = destination.stat().st_size if destination.exists() else 0
if initial > source.stat().st_size:
    destination.unlink()
    initial = 0
with source.open("rb") as incoming:
    incoming.seek(initial)
    with destination.open("ab" if initial else "wb", buffering=0) as outgoing:
        copied = initial
        while chunk := incoming.read(65536):
            outgoing.write(chunk)
            copied += len(chunk)
            print(f"\r{copied:,} 100% 1.00MB/s 0:00:00", end="", flush=True)
            time.sleep(float(os.environ.get("FAKE_RSYNC_DELAY", "0")))
"""


def write_executable(path: Path, content: str) -> None:
    path.write_text(content.replace("__PYTHON__", sys.executable), encoding="utf-8")
    path.chmod(0o755)


def install_process_fakes(fake_bin: Path) -> None:
    fake_bin.mkdir()
    write_executable(fake_bin / "ssh", FAKE_SSH)
    write_executable(fake_bin / "sudo", FAKE_SUDO)
    write_executable(fake_bin / "rsync", FAKE_RSYNC)


def structured(result: CallToolResult) -> dict[str, object]:
    assert result.structured_content is not None
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def wait_for_transfer(
    session: ClientSession, operation_id: str
) -> dict[str, object]:
    async with asyncio.timeout(5):
        while True:
            response = await session.call_tool(
                "transfer_status", {"operation_id": operation_id}
            )
            assert isinstance(response, CallToolResult)
            data = structured(response)
            result = data["result"]
            assert isinstance(result, dict)
            if result["state"] in {"completed", "failed", "cancelled"}:
                return result
            await asyncio.sleep(0.01)


def test_tool_schemas_are_strict_and_only_connect_selects_authority() -> None:
    expected = {
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
    assert {definition.name for definition in TOOL_DEFINITIONS} == expected
    for definition in TOOL_DEFINITIONS:
        tool = definition.protocol_tool()
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema is not None
        assert tool.output_schema["additionalProperties"] is False
        properties = set(tool.input_schema.get("properties", {}))
        assert properties.isdisjoint(
            {"target", "identity_file", "control_path", "password", "ssh_options"}
        )
        if definition.name == "connect":
            assert properties == {"ssh_alias", "host", "user", "port"}
            assert len(tool.input_schema["oneOf"]) == 2
        else:
            assert properties.isdisjoint({"ssh_alias", "host", "user", "port"})

    mutating = {
        definition.name: definition.annotations
        for definition in TOOL_DEFINITIONS
        if definition.name in {"exec", "sudo_exec", "download_start", "upload_start"}
    }
    assert all(annotation.read_only_hint is False for annotation in mutating.values())
    assert all(annotation.destructive_hint is True for annotation in mutating.values())
    assert "starts disconnected" in SERVER_INSTRUCTIONS[:512]
    assert "Never request or pass a password" in SERVER_INSTRUCTIONS[:768]


def test_codex_example_is_disabled_and_exposes_the_complete_toolset() -> None:
    example = Path(__file__).resolve().parents[1] / "doc/examples/codex-config.toml"
    config = tomllib.loads(example.read_text(encoding="utf-8"))
    server = config["mcp_servers"]["remote_machine"]

    assert server["command"] == "remote-ssh-mcp"
    assert "--target" not in server["args"]
    assert server["args"][0] == "--local-root"
    assert server["enabled"] is False
    assert server["startup_timeout_sec"] >= 120
    assert set(server["enabled_tools"]) == {
        definition.name for definition in TOOL_DEFINITIONS
    }
    for name in (
        "connect",
        "disconnect",
        "exec",
        "sudo_exec",
        "download_start",
        "upload_start",
        "transfer_cancel",
    ):
        assert server["tools"][name]["approval_mode"] == "prompt"


def test_claude_code_examples_match_tool_names_and_approval_policy() -> None:
    root = Path(__file__).resolve().parents[1] / "doc/examples"
    mcp_example = json.loads(
        (root / "claude-code-mcp.json").read_text(encoding="utf-8")
    )
    settings_example = json.loads(
        (root / "claude-code-settings.json").read_text(encoding="utf-8")
    )

    server = mcp_example["mcpServers"]["remote_machine"]
    assert server == {
        "type": "stdio",
        "command": "remote-ssh-mcp",
        "args": ["--local-root", "/absolute/path/to/allowed-local-root"],
        "env": {},
    }

    prefix = "mcp__remote_machine__"
    permissions = settings_example["permissions"]
    allowed = {name.removeprefix(prefix) for name in permissions["allow"]}
    prompted = {name.removeprefix(prefix) for name in permissions["ask"]}
    assert allowed.isdisjoint(prompted)
    assert allowed | prompted == {definition.name for definition in TOOL_DEFINITIONS}
    assert prompted == {
        "connect",
        "disconnect",
        "exec",
        "sudo_exec",
        "download_start",
        "upload_start",
        "transfer_cancel",
    }


@pytest.mark.asyncio
async def test_application_starts_disconnected_and_reports_connecting(
    runtime_config: RuntimeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_start(master: OpenSSHMaster) -> None:
        master.state = ConnectionState.STARTING
        entered.set()
        await release.wait()
        master.state = ConnectionState.READY

    monkeypatch.setattr(OpenSSHMaster, "start", delayed_start)
    app = RemoteSSHApplication(runtime_config)
    await app.start()
    assert (await app.connection_status())["state"] == "disconnected"

    connecting = asyncio.create_task(app.connect(ConnectInput(ssh_alias="test-target")))
    await entered.wait()
    assert (await app.connection_status())["state"] == "starting"
    release.set()
    assert (await connecting)["state"] == "ready"
    assert (await app.disconnect())["state"] == "disconnected"


@pytest.mark.asyncio
async def test_real_stdio_protocol_and_lifecycle_with_process_fakes(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    install_process_fakes(fake_bin)

    auth_count = tmp_path / "auth-count"
    master_pid_file = tmp_path / "master-pid"
    control_path_file = tmp_path / "control-path"
    sudo_log = tmp_path / "sudo.log"
    rsync_log = tmp_path / "rsync.log"
    stderr_log = tmp_path / "server.stderr"
    remote_source = tmp_path / "remote source.bin"
    remote_source.write_bytes(b"0123456789abcdef" * 10_000)
    upload_source = tmp_path / "upload source.bin"
    upload_source.write_bytes(b"upload-data" * 1000)
    cancel_source = tmp_path / "cancel source.bin"
    cancel_source.write_bytes(b"cancel-data" * 800_000)
    (tmp_path / "downloads").mkdir()
    launcher = Path(__file__).resolve().parents[1] / "remote-ssh-mcp"
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_SSH_AUTH_COUNT": str(auth_count),
        "FAKE_SSH_MASTER_PID": str(master_pid_file),
        "FAKE_SSH_CONTROL_PATH": str(control_path_file),
        "FAKE_SUDO_LOG": str(sudo_log),
        "FAKE_RSYNC_LOG": str(rsync_log),
        "FAKE_RSYNC_DELAY": "0.002",
    }
    parameters = StdioServerParameters(
        command=str(launcher),
        args=[
            "--local-root",
            str(tmp_path),
            "--connect-timeout",
            "3",
            "--command-timeout",
            "3",
            "--max-output-bytes",
            "4096",
            "--log-level",
            "ERROR",
        ],
        env=environment,
        cwd=tmp_path,
    )

    with stderr_log.open("w+", encoding="utf-8") as errlog:
        async with (
            stdio_client(parameters, errlog=errlog) as (read, write),
            ClientSession(read, write, read_timeout_seconds=10) as session,
        ):
            initialized = await session.initialize()
            assert initialized.server_info.name == "remote-ssh-mcp"
            assert initialized.instructions == SERVER_INSTRUCTIONS

            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert set(tools) == {definition.name for definition in TOOL_DEFINITIONS}
            assert tools["exec"].annotations is not None
            assert tools["exec"].annotations.destructive_hint is True

            assert not auth_count.exists()
            status = await session.call_tool("connection_status", {})
            assert isinstance(status, CallToolResult)
            assert structured(status)["result"] == {  # type: ignore[comparison-overlap]
                "state": "disconnected",
                "mode": None,
                "target": None,
                "ssh_alias": None,
                "host": None,
                "user": None,
                "port": None,
                "master_pid": None,
            }
            before_connect = await session.call_tool("exec", {"command": "true"})
            assert isinstance(before_connect, CallToolResult)
            assert before_connect.is_error
            assert structured(before_connect)["error"]["code"] == "not_connected"  # type: ignore[index]

            invalid_connect = await session.call_tool(
                "connect",
                {"ssh_alias": "test-target", "host": "other", "user": "deploy"},
            )
            assert isinstance(invalid_connect, CallToolResult)
            assert invalid_connect.is_error
            assert structured(invalid_connect)["error"]["code"] == "invalid_arguments"  # type: ignore[index]
            assert not auth_count.exists()

            connected = await session.call_tool("connect", {"ssh_alias": "test-target"})
            assert isinstance(connected, CallToolResult)
            connection = structured(connected)["result"]
            assert connection["state"] == "ready"  # type: ignore[index]
            assert connection["mode"] == "alias"  # type: ignore[index]
            assert connection["ssh_alias"] == "test-target"  # type: ignore[index]
            assert auth_count.read_text(encoding="utf-8") == "1"

            duplicate = await session.call_tool("connect", {"ssh_alias": "test-target"})
            assert isinstance(duplicate, CallToolResult)
            assert duplicate.is_error
            assert structured(duplicate)["error"]["code"] == "already_connected"  # type: ignore[index]

            first_master_pid = int(master_pid_file.read_text(encoding="utf-8"))
            first_control_path = Path(control_path_file.read_text(encoding="utf-8"))

            status = await session.call_tool("connection_status", {})
            assert isinstance(status, CallToolResult)
            assert structured(status)["result"]["state"] == "ready"  # type: ignore[index]

            executed = await session.call_tool(
                "exec",
                {"command": "printf output; printf error >&2; exit 7"},
            )
            assert isinstance(executed, CallToolResult)
            command = structured(executed)["result"]
            assert command["exit_code"] == 7  # type: ignore[index]
            assert command["stdout"]["data"] == "output"  # type: ignore[index]
            assert command["stderr"]["data"] == "error"  # type: ignore[index]

            sleep_pid_file = tmp_path / "cancelled-command-pid"
            long_call = asyncio.create_task(
                session.call_tool(
                    "exec",
                    {
                        "command": (
                            f"echo $$ > {shlex.quote(str(sleep_pid_file))}; sleep 10"
                        )
                    },
                )
            )
            async with asyncio.timeout(2):
                while not sleep_pid_file.exists():
                    await asyncio.sleep(0.01)
            sleep_pid = int(sleep_pid_file.read_text(encoding="utf-8"))
            long_call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await long_call
            async with asyncio.timeout(2):
                while True:
                    try:
                        os.kill(sleep_pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
            still_ready = await session.call_tool("connection_status", {})
            assert isinstance(still_ready, CallToolResult)
            assert structured(still_ready)["result"]["state"] == "ready"  # type: ignore[index]

            metadata = await session.call_tool(
                "stat", {"remote_path": str(remote_source)}
            )
            assert isinstance(metadata, CallToolResult)
            assert (
                structured(metadata)["result"]["size"] == remote_source.stat().st_size
            )  # type: ignore[index]

            listing = await session.call_tool(
                "list_directory", {"remote_path": str(tmp_path)}
            )
            assert isinstance(listing, CallToolResult)
            assert structured(listing)["result"]["count"] >= 3  # type: ignore[index,operator]

            ranged = await session.call_tool(
                "read_file_range",
                {"remote_path": str(remote_source), "offset": 2, "max_bytes": 5},
            )
            assert isinstance(ranged, CallToolResult)
            assert structured(ranged)["result"]["data"] == "23456"  # type: ignore[index]

            default_range = await session.call_tool(
                "read_file_range", {"remote_path": str(remote_source)}
            )
            assert isinstance(default_range, CallToolResult)
            assert structured(default_range)["result"]["bytes_read"] == 4096  # type: ignore[index]

            privileged = await session.call_tool(
                "sudo_exec", {"command": "printf privileged"}
            )
            assert isinstance(privileged, CallToolResult)
            assert structured(privileged)["result"]["stdout"]["data"] == "privileged"  # type: ignore[index]

            download = await session.call_tool(
                "download_start",
                {
                    "remote_path": str(remote_source),
                    "local_path": "downloads/result.bin",
                },
            )
            assert isinstance(download, CallToolResult)
            download_id = structured(download)["result"]["operation_id"]  # type: ignore[index]
            download_result = await wait_for_transfer(session, str(download_id))
            assert download_result["state"] == "completed"
            assert (
                tmp_path / "downloads/result.bin"
            ).read_bytes() == remote_source.read_bytes()

            remote_upload = tmp_path / "remote uploaded.bin"
            upload = await session.call_tool(
                "upload_start",
                {
                    "local_path": "upload source.bin",
                    "remote_path": str(remote_upload),
                },
            )
            assert isinstance(upload, CallToolResult)
            upload_id = structured(upload)["result"]["operation_id"]  # type: ignore[index]
            upload_result = await wait_for_transfer(session, str(upload_id))
            assert upload_result["state"] == "completed"
            assert remote_upload.read_bytes() == upload_source.read_bytes()

            cancellable = await session.call_tool(
                "download_start",
                {
                    "remote_path": str(cancel_source),
                    "local_path": "downloads/cancelled.bin",
                },
            )
            assert isinstance(cancellable, CallToolResult)
            cancel_id = structured(cancellable)["result"]["operation_id"]  # type: ignore[index]
            cancelled = await session.call_tool(
                "transfer_cancel", {"operation_id": cancel_id}
            )
            assert isinstance(cancelled, CallToolResult)
            assert structured(cancelled)["result"]["state"] == "cancelled"  # type: ignore[index]

            transfers = await session.call_tool("transfer_list", {})
            assert isinstance(transfers, CallToolResult)
            assert len(structured(transfers)["result"]) == 3  # type: ignore[arg-type]

            invalid = await session.call_tool(
                "stat", {"remote_path": str(remote_source), "target": "other"}
            )
            assert isinstance(invalid, CallToolResult)
            assert invalid.is_error
            assert structured(invalid)["error"]["code"] == "invalid_arguments"  # type: ignore[index]

            traversal = await session.call_tool(
                "download_start",
                {"remote_path": str(remote_source), "local_path": "../escape"},
            )
            assert isinstance(traversal, CallToolResult)
            assert traversal.is_error
            assert structured(traversal)["error"]["code"] == "invalid_local_path"  # type: ignore[index]

            disconnect_pid_file = tmp_path / "disconnect-command-pid"
            active_at_disconnect = asyncio.create_task(
                session.call_tool(
                    "exec",
                    {
                        "command": (
                            f"echo $$ > {shlex.quote(str(disconnect_pid_file))}; "
                            "exec sleep 10"
                        )
                    },
                )
            )
            async with asyncio.timeout(2):
                while not disconnect_pid_file.exists():
                    await asyncio.sleep(0.01)
            disconnect_pid = int(disconnect_pid_file.read_text(encoding="utf-8"))
            disconnected = await session.call_tool("disconnect", {})
            assert isinstance(disconnected, CallToolResult)
            assert structured(disconnected)["result"]["state"] == "disconnected"  # type: ignore[index]
            interrupted = await asyncio.wait_for(active_at_disconnect, timeout=5)
            assert isinstance(interrupted, CallToolResult)
            assert interrupted.is_error
            assert structured(interrupted)["error"]["code"] == "connection_lost"  # type: ignore[index]
            with pytest.raises(ProcessLookupError):
                os.kill(disconnect_pid, 0)
            with pytest.raises(ProcessLookupError):
                os.kill(first_master_pid, 0)
            assert not first_control_path.exists()
            assert not first_control_path.parent.exists()

            disconnected_again = await session.call_tool("disconnect", {})
            assert isinstance(disconnected_again, CallToolResult)
            assert structured(disconnected_again)["result"]["state"] == "disconnected"  # type: ignore[index]

            reconnected = await session.call_tool(
                "connect",
                {"host": "host.example", "user": "deploy", "port": 2222},
            )
            assert isinstance(reconnected, CallToolResult)
            direct = structured(reconnected)["result"]
            assert direct["state"] == "ready"  # type: ignore[index]
            assert direct["mode"] == "direct"  # type: ignore[index]
            assert direct["host"] == "host.example"  # type: ignore[index]
            assert direct["user"] == "deploy"  # type: ignore[index]
            assert direct["port"] == 2222  # type: ignore[index]
            assert auth_count.read_text(encoding="utf-8") == "2"
            second_master_pid = int(master_pid_file.read_text(encoding="utf-8"))
            os.kill(second_master_pid, signal.SIGTERM)
            async with asyncio.timeout(2):
                while True:
                    try:
                        os.kill(second_master_pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
            lost = await session.call_tool("connection_status", {})
            assert isinstance(lost, CallToolResult)
            assert structured(lost)["result"]["state"] == "lost"  # type: ignore[index]
            blocked_reconnect = await session.call_tool(
                "connect", {"ssh_alias": "test-target"}
            )
            assert isinstance(blocked_reconnect, CallToolResult)
            assert blocked_reconnect.is_error
            assert (
                structured(blocked_reconnect)["error"]["code"] == "disconnect_required"
            )  # type: ignore[index]
            assert auth_count.read_text(encoding="utf-8") == "2"
            final_disconnect = await session.call_tool("disconnect", {})
            assert isinstance(final_disconnect, CallToolResult)
            assert structured(final_disconnect)["result"]["state"] == "disconnected"  # type: ignore[index]

        errlog.flush()
        errlog.seek(0)
        diagnostics = errlog.read()

    assert auth_count.read_text(encoding="utf-8") == "2"
    sudo_args = json.loads(sudo_log.read_text(encoding="utf-8").splitlines()[0])
    assert sudo_args[:3] == ["-n", "-k", "--"]
    rsync_calls = [
        json.loads(line) for line in rsync_log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rsync_calls) >= 2
    assert all("--append-verify" in call for call in rsync_calls)
    assert all(any("ProxyCommand=" in arg for arg in call) for call in rsync_calls)
    assert '"jsonrpc"' not in diagnostics

    master_pid = int(master_pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(master_pid, 0)
    control_path = Path(control_path_file.read_text(encoding="utf-8"))
    assert not control_path.exists()
    assert not control_path.parent.exists()


@pytest.mark.asyncio
async def test_sigterm_cleans_master_with_stdin_still_open(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    install_process_fakes(fake_bin)
    auth_count = tmp_path / "auth-count"
    master_pid_file = tmp_path / "master-pid"
    control_path_file = tmp_path / "control-path"
    launcher = Path(__file__).resolve().parents[1] / "remote-ssh-mcp"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "FAKE_SSH_AUTH_COUNT": str(auth_count),
            "FAKE_SSH_MASTER_PID": str(master_pid_file),
            "FAKE_SSH_CONTROL_PATH": str(control_path_file),
            "FAKE_SUDO_LOG": str(tmp_path / "unused-sudo.log"),
            "FAKE_RSYNC_LOG": str(tmp_path / "unused-rsync.log"),
        }
    )
    process = await asyncio.create_subprocess_exec(
        str(launcher),
        "--local-root",
        str(tmp_path),
        "--connect-timeout",
        "3",
        "--log-level",
        "ERROR",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=tmp_path,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "lifecycle-test", "version": "1"},
                    },
                }
            ).encode("utf-8")
            + b"\n"
        )
        await process.stdin.drain()
        initialized = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert initialized["id"] == 1
        assert not control_path_file.exists()
        assert not auth_count.exists()
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.write(
            b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
            b'{"name":"connect","arguments":{"ssh_alias":"test-target"}}}\n'
        )
        await process.stdin.drain()
        connected = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert connected["id"] == 2
        assert connected["result"]["structuredContent"]["result"]["state"] == "ready"
        async with asyncio.timeout(5):
            while not control_path_file.exists():
                if process.returncode is not None:
                    raise AssertionError(
                        f"server exited early with {process.returncode}"
                    )
                await asyncio.sleep(0.01)
            control_path = Path(control_path_file.read_text(encoding="utf-8"))
            while not control_path.exists():
                await asyncio.sleep(0.01)

        process.send_signal(signal.SIGTERM)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    assert process.returncode == 0
    assert stdout == b""
    assert b'"jsonrpc"' not in stderr
    assert auth_count.read_text(encoding="utf-8") == "1"
    master_pid = int(master_pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(master_pid, 0)
    assert not control_path.exists()
    assert not control_path.parent.exists()


@pytest.mark.asyncio
async def test_master_start_failure_is_concise_and_cleans_runtime(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    install_process_fakes(fake_bin)
    control_path_file = tmp_path / "control-path"
    launcher = Path(__file__).resolve().parents[1] / "remote-ssh-mcp"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "FAKE_SSH_AUTH_COUNT": str(tmp_path / "auth-count"),
            "FAKE_SSH_MASTER_PID": str(tmp_path / "master-pid"),
            "FAKE_SSH_CONTROL_PATH": str(control_path_file),
            "FAKE_SSH_FAIL_MASTER": "1",
            "FAKE_SUDO_LOG": str(tmp_path / "unused-sudo.log"),
            "FAKE_RSYNC_LOG": str(tmp_path / "unused-rsync.log"),
        }
    )
    parameters = StdioServerParameters(
        command=str(launcher),
        args=[
            "--local-root",
            str(tmp_path),
            "--connect-timeout",
            "3",
            "--log-level",
            "ERROR",
        ],
        env=environment,
        cwd=tmp_path,
    )
    stderr_log = tmp_path / "failed.stderr"
    with stderr_log.open("w+", encoding="utf-8") as errlog:
        async with (
            stdio_client(parameters, errlog=errlog) as (read, write),
            ClientSession(read, write, read_timeout_seconds=10) as session,
        ):
            await session.initialize()
            failed = await session.call_tool("connect", {"ssh_alias": "test-target"})
            assert isinstance(failed, CallToolResult)
            assert failed.is_error
            assert structured(failed)["error"]["code"] == "connection_start_failed"  # type: ignore[index]
            status = await session.call_tool("connection_status", {})
            assert isinstance(status, CallToolResult)
            assert structured(status)["result"]["state"] == "disconnected"  # type: ignore[index]
        errlog.flush()
        errlog.seek(0)
        diagnostics = errlog.read()

    assert "Traceback" not in diagnostics
    control_path = Path(control_path_file.read_text(encoding="utf-8"))
    assert not control_path.exists()
    assert not control_path.parent.exists()
