from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from remote_ssh_mcp import sessions as sessions_module
from remote_ssh_mcp.config import ConnectionSpec, RuntimeConfig
from remote_ssh_mcp.errors import RemoteMCPError
from remote_ssh_mcp.local_paths import LocalPathPolicy
from remote_ssh_mcp.master import ConnectionState, OpenSSHMaster
from remote_ssh_mcp.sessions import (
    ServerIdentity,
    SessionManager,
    resolve_server_identity,
)

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

FAKE_SSH_CONFIG = r"""#!{python}
import os
import sys
import time

args = sys.argv[1:]
assert args[0] == "-G", args
with open(os.environ["FAKE_SSH_G_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")
mode = os.environ.get("FAKE_SSH_G_MODE", "ok")
if mode == "fail":
    raise SystemExit(255)
if mode == "slow":
    time.sleep(30)
if mode == "huge":
    sys.stdout.write("x" * 2_000_000)
    raise SystemExit(0)
destination = args[-1]
port = args[args.index("-p") + 1] if "-p" in args else "22"
if mode == "missing":
    print("port " + port)
    raise SystemExit(0)
print("user someone")
print("hostname " + os.environ.get("FAKE_SSH_G_HOST", destination.upper()))
print("port " + port)
print("hostname ignored.example")
"""


class FakeStarts:
    """Replace authentication with controllable in-process master startup."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}
        self.failures: dict[str, RemoteMCPError] = {}
        self.order: list[str] = []
        self.active = 0
        self.max_active = 0

    def gate(self, destination: str) -> asyncio.Event:
        return self.gates.setdefault(destination, asyncio.Event())

    def entered_event(self, destination: str) -> asyncio.Event:
        return self.entered.setdefault(destination, asyncio.Event())

    async def start(self, master: OpenSSHMaster) -> None:
        destination = master.connection.destination
        master.state = ConnectionState.STARTING
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.order.append(destination)
        self.entered_event(destination).set()
        try:
            gate = self.gates.get(destination)
            if gate is not None:
                await gate.wait()
            failure = self.failures.get(destination)
            if failure is not None:
                raise failure
            master.state = ConnectionState.READY
        finally:
            self.active -= 1


@pytest.fixture
def fake_starts(monkeypatch: pytest.MonkeyPatch) -> FakeStarts:
    starts = FakeStarts()

    async def start(master: OpenSSHMaster) -> None:
        try:
            await starts.start(master)
        except BaseException:
            await master.close()
            raise

    async def ensure_ready(master: OpenSSHMaster) -> None:
        if master.state is not ConnectionState.READY:
            raise RemoteMCPError("connection_lost", "fake master is not ready")

    async def resolve(
        _config: RuntimeConfig, connection: ConnectionSpec
    ) -> ServerIdentity:
        # Aliases named "<host>-alias" resolve to the same server as <host>.
        host = connection.destination.removesuffix("-alias")
        return ServerIdentity(host=host.lower(), port=connection.port or 22)

    monkeypatch.setattr(OpenSSHMaster, "start", start)
    monkeypatch.setattr(OpenSSHMaster, "ensure_ready", ensure_ready)
    monkeypatch.setattr(sessions_module, "resolve_server_identity", resolve)
    return starts


@pytest.fixture
def manager_factory(
    runtime_config: RuntimeConfig,
) -> Callable[..., SessionManager]:
    def create(**overrides: object) -> SessionManager:
        config = replace(runtime_config, **overrides)  # type: ignore[arg-type]
        paths = LocalPathPolicy(config.repository_root)
        paths.initialize()
        return SessionManager(config, paths)

    return create


def alias(name: str) -> ConnectionSpec:
    return ConnectionSpec.from_alias(name)


@pytest.mark.asyncio
async def test_connect_issues_public_id_and_key_exactly_once(
    fake_starts: FakeStarts,
    manager_factory: Callable[..., SessionManager],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    manager = manager_factory()

    session, key = await manager.connect(alias("web1"))

    assert ID_PATTERN.fullmatch(session.session_id)
    assert KEY_PATTERN.fullmatch(key)
    assert key not in repr(session)
    assert session.key_digest.hex() not in repr(session)
    status = session.status()
    assert status["state"] == "ready"
    assert status["server_host"] == "web1"
    assert status["server_port"] == 22
    assert status["target"] == "web1"
    assert key not in repr(status)
    listed = await manager.list_sessions()
    assert listed == [status]
    assert key not in repr(listed)

    authorized, services = manager.authorize(session.session_id, key)
    assert authorized is session
    assert services is session.services
    assert session.last_used_at is not None
    assert key not in caplog.text
    await manager.close()


@pytest.mark.asyncio
async def test_every_operation_requires_the_exact_session_key(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory()
    first, first_key = await manager.connect(alias("web1"))
    second, second_key = await manager.connect(alias("web2"))
    assert first.session_id != second.session_id
    assert first_key != second_key

    with pytest.raises(RemoteMCPError) as foreign:
        manager.authorize(first.session_id, second_key)
    assert foreign.value.code == "invalid_session_key"
    with pytest.raises(RemoteMCPError) as missing:
        manager.authorize("0" * 32, first_key)
    assert missing.value.code == "session_not_found"
    with pytest.raises(RemoteMCPError) as altered:
        manager.authorize(
            first.session_id, first_key[:-1] + ("B" if first_key[-1] == "A" else "A")
        )
    assert altered.value.code == "invalid_session_key"
    assert first.last_used_at is None

    assert manager.authorize(second.session_id, second_key)[0] is second
    await manager.close()


@pytest.mark.asyncio
async def test_one_session_per_server_until_it_is_disconnected_without_key(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory()
    first, first_key = await manager.connect(alias("web1"))

    for duplicate in (
        alias("web1"),
        alias("web1-alias"),
        ConnectionSpec.from_direct("WEB1", "other-user", 22),
    ):
        with pytest.raises(RemoteMCPError) as raised:
            await manager.connect(duplicate)
        assert raised.value.code == "already_connected"
        assert first.session_id in raised.value.message
    other_port, _ = await manager.connect(ConnectionSpec.from_direct("web1", "u", 2222))
    assert other_port.server == ServerIdentity("web1", 2222)

    first.master.state = ConnectionState.LOST
    with pytest.raises(RemoteMCPError) as lost:
        await manager.connect(alias("web1"))
    assert lost.value.code == "already_connected"

    closed = await manager.disconnect(first.session_id)
    assert closed["state"] == "closed"
    assert closed["master_pid"] is None
    with pytest.raises(RemoteMCPError) as stale:
        manager.authorize(first.session_id, first_key)
    assert stale.value.code == "session_not_found"
    with pytest.raises(RemoteMCPError) as again:
        await manager.disconnect(first.session_id)
    assert again.value.code == "session_not_found"

    replacement, replacement_key = await manager.connect(alias("web1-alias"))
    assert replacement.session_id != first.session_id
    assert replacement_key != first_key
    await manager.close()


@pytest.mark.asyncio
async def test_duplicate_server_is_rejected_while_first_session_authenticates(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory()
    fake_starts.gate("web1")
    pending = asyncio.create_task(manager.connect(alias("web1")))
    await fake_starts.entered_event("web1").wait()

    listed = await manager.list_sessions()
    assert [value["state"] for value in listed] == ["starting"]
    with pytest.raises(RemoteMCPError) as raised:
        await manager.connect(alias("web1-alias"))
    assert raised.value.code == "already_connected"

    fake_starts.gate("web1").set()
    session, _key = await pending
    assert session.status()["state"] == "ready"
    await manager.close()


@pytest.mark.asyncio
async def test_authentication_is_serialized_but_ready_sessions_are_parallel(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory()
    fake_starts.gate("web1")
    fake_starts.gate("web2")
    first = asyncio.create_task(manager.connect(alias("web1")))
    await fake_starts.entered_event("web1").wait()
    second = asyncio.create_task(manager.connect(alias("web2")))
    await asyncio.sleep(0.05)

    assert fake_starts.order == ["web1"]
    states = {
        value["target"]: value["state"] for value in await manager.list_sessions()
    }
    assert states == {"web1": "starting", "web2": "starting"}
    fake_starts.gate("web1").set()
    await first
    await fake_starts.entered_event("web2").wait()
    fake_starts.gate("web2").set()
    await second

    assert fake_starts.order == ["web1", "web2"]
    assert fake_starts.max_active == 1
    assert {value["state"] for value in await manager.list_sessions()} == {"ready"}
    await manager.close()


@pytest.mark.asyncio
async def test_disconnect_by_id_cancels_a_starting_session(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory()
    fake_starts.gate("web1")
    pending = asyncio.create_task(manager.connect(alias("web1")))
    await fake_starts.entered_event("web1").wait()
    [listed] = await manager.list_sessions()

    with pytest.raises(RemoteMCPError) as not_ready:
        manager.authorize(str(listed["session_id"]), "A" * 43)
    assert not_ready.value.code == "invalid_session_key"

    closed = await manager.disconnect(str(listed["session_id"]))
    assert closed["state"] == "closed"
    with pytest.raises(RemoteMCPError) as raised:
        await pending
    assert raised.value.code == "session_closed"
    assert await manager.list_sessions() == []
    assert fake_starts.active == 0

    fake_starts.gates.pop("web1")
    session, _key = await manager.connect(alias("web1"))
    assert session.status()["state"] == "ready"
    await manager.close()


@pytest.mark.asyncio
async def test_starting_session_reports_not_ready_for_its_own_key(
    fake_starts: FakeStarts,
    manager_factory: Callable[..., SessionManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = manager_factory()
    issued: list[str] = []
    original = sessions_module.secrets.token_urlsafe

    def recording_token(length: int) -> str:
        value = original(length)
        issued.append(value)
        return value

    monkeypatch.setattr(sessions_module.secrets, "token_urlsafe", recording_token)
    fake_starts.gate("web1")
    pending = asyncio.create_task(manager.connect(alias("web1")))
    await fake_starts.entered_event("web1").wait()
    [listed] = await manager.list_sessions()

    with pytest.raises(RemoteMCPError) as raised:
        manager.authorize(str(listed["session_id"]), issued[0])
    assert raised.value.code == "session_not_ready"

    fake_starts.gate("web1").set()
    _session, key = await pending
    assert key == issued[0]
    await manager.close()


@pytest.mark.asyncio
async def test_cancelled_connect_closes_and_forgets_its_session(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory()
    fake_starts.gate("web1")
    pending = asyncio.create_task(manager.connect(alias("web1")))
    await fake_starts.entered_event("web1").wait()

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert await manager.list_sessions() == []
    assert fake_starts.active == 0
    fake_starts.gates.pop("web1")
    session, _key = await manager.connect(alias("web1"))
    assert session.status()["state"] == "ready"
    await manager.close()


@pytest.mark.asyncio
async def test_failed_authentication_releases_the_server(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory()
    fake_starts.failures["web1"] = RemoteMCPError(
        "connection_start_failed", "SSH master exited with status 255"
    )

    with pytest.raises(RemoteMCPError) as raised:
        await manager.connect(alias("web1"))

    assert raised.value.code == "connection_start_failed"
    assert await manager.list_sessions() == []
    fake_starts.failures.clear()
    session, _key = await manager.connect(alias("web1"))
    assert session.status()["state"] == "ready"
    await manager.close()


@pytest.mark.asyncio
async def test_session_limit_counts_starting_and_ready_sessions(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory(max_sessions=2)
    await manager.connect(alias("web1"))
    fake_starts.gate("web2")
    pending = asyncio.create_task(manager.connect(alias("web2")))
    await fake_starts.entered_event("web2").wait()

    with pytest.raises(RemoteMCPError) as raised:
        await manager.connect(alias("web3"))
    assert raised.value.code == "session_limit_reached"

    fake_starts.gate("web2").set()
    await pending
    await manager.close()


@pytest.mark.asyncio
async def test_closing_session_rejects_work_and_close_refuses_new_sessions(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory()
    first, first_key = await manager.connect(alias("web1"))
    second, _second_key = await manager.connect(alias("web2"))
    first.closing = True
    with pytest.raises(RemoteMCPError) as closing:
        manager.authorize(first.session_id, first_key)
    assert closing.value.code == "session_closing"
    first.closing = False

    await manager.close()

    assert first.master.state is ConnectionState.CLOSED
    assert second.master.state is ConnectionState.CLOSED
    assert await manager.list_sessions() == []
    with pytest.raises(RemoteMCPError) as refused:
        await manager.connect(alias("web3"))
    assert refused.value.code == "server_closing"


@pytest.mark.asyncio
async def test_lost_session_stays_listed_until_explicit_disconnect(
    fake_starts: FakeStarts, manager_factory: Callable[..., SessionManager]
) -> None:
    manager = manager_factory()
    session, key = await manager.connect(alias("web1"))
    session.master.state = ConnectionState.LOST

    [listed] = await manager.list_sessions()
    assert listed["state"] == "lost"
    assert manager.authorize(session.session_id, key)[0] is session
    assert fake_starts.order == ["web1"]

    await manager.disconnect(session.session_id)
    assert await manager.list_sessions() == []
    await manager.close()


def fake_ssh_config(tmp_path: Path) -> Path:
    path = tmp_path / "fake-ssh-config"
    path.write_text(FAKE_SSH_CONFIG.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_server_identity_uses_effective_openssh_configuration(
    runtime_config: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "ssh-g.log"
    monkeypatch.setenv("FAKE_SSH_G_LOG", str(log))
    config = replace(runtime_config, ssh_path=fake_ssh_config(tmp_path))

    by_alias = await resolve_server_identity(config, alias("Web1"))
    monkeypatch.setenv("FAKE_SSH_G_HOST", "web1")
    direct = await resolve_server_identity(
        config, ConnectionSpec.from_direct("10.0.0.5", "deploy", 2222)
    )

    assert by_alias == ServerIdentity("web1", 22)
    assert direct == ServerIdentity("web1", 2222)
    assert log.read_text(encoding="utf-8").splitlines() == [
        "-G -- Web1",
        "-G -l deploy -p 2222 -- 10.0.0.5",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fail", "missing", "huge"])
async def test_unresolvable_configuration_fails_before_authentication(
    runtime_config: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("FAKE_SSH_G_LOG", str(tmp_path / "ssh-g.log"))
    monkeypatch.setenv("FAKE_SSH_G_MODE", mode)
    config = replace(runtime_config, ssh_path=fake_ssh_config(tmp_path))

    with pytest.raises(RemoteMCPError) as raised:
        await resolve_server_identity(config, alias("web1"))

    assert raised.value.code == "connection_start_failed"
    assert raised.value.message == (
        "SSH configuration for the target could not be resolved"
    )


@pytest.mark.asyncio
async def test_configuration_resolution_is_bounded_in_time(
    runtime_config: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_SSH_G_LOG", str(tmp_path / "ssh-g.log"))
    monkeypatch.setenv("FAKE_SSH_G_MODE", "slow")
    monkeypatch.setattr(sessions_module, "RESOLVE_TIMEOUT", 0.3)
    config = replace(runtime_config, ssh_path=fake_ssh_config(tmp_path))

    async with asyncio.timeout(5):
        with pytest.raises(RemoteMCPError) as raised:
            await resolve_server_identity(config, alias("web1"))

    assert raised.value.code == "connection_start_failed"
