from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from tests.live_support import process as live_process
from tests.live_support.process import LiveResources


def result(
    command: list[str], returncode: int, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def install_fake_process(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> None:
    def fake(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        return handler(command)

    monkeypatch.setattr(live_process, "run_process", fake)
    monkeypatch.setattr(live_process.time, "sleep", lambda _seconds: None)


def owned_labels(name: str, owner: str) -> str:
    return json.dumps(
        {
            live_process.RUN_LABEL: name,
            live_process.OWNER_LABEL: owner,
        }
    )


def test_cleanup_accepts_failed_remove_after_resource_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exists_results = iter((0, 1))

    def handler(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["container", "exists"]:
            return result(command, next(exists_results))
        if command[1:3] == ["container", "inspect"]:
            return result(command, 0, stdout=owned_labels("target", "live-target"))
        assert command[1:3] == ["rm", "--force"]
        return result(command, 125, stderr="already being removed")

    install_fake_process(monkeypatch, handler)
    resources = LiveResources("podman", target_name="target")

    assert resources.cleanup()
    assert resources.target_name is None


def test_cleanup_retries_owned_network_until_it_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exists_results = iter((0, 0, 1))
    removals = 0

    def handler(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal removals
        if command[1:3] == ["network", "exists"]:
            return result(command, next(exists_results))
        if command[1:3] == ["network", "inspect"]:
            return result(command, 0, stdout=owned_labels("network", "live-network"))
        assert command[1:4] == ["network", "rm", "--force"]
        removals += 1
        return result(command, 125 if removals == 1 else 0)

    install_fake_process(monkeypatch, handler)
    resources = LiveResources("podman", network_name="network")

    assert resources.cleanup()
    assert removals == 2
    assert resources.network_name is None


def test_cleanup_never_removes_resource_with_foreign_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def handler(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["container", "exists"]:
            return result(command, 0)
        if command[1:3] == ["container", "inspect"]:
            return result(command, 0, stdout=owned_labels("other", "live-target"))
        raise AssertionError(f"unexpected removal command: {command}")

    install_fake_process(monkeypatch, handler)
    resources = LiveResources("podman", target_name="target")

    assert not resources.cleanup()
    assert resources.target_name == "target"
    assert all(command[1] != "rm" for command in commands)
