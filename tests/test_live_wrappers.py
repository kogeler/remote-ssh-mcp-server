from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# The harness drives Podman on the machine, which a container cannot do.
pytestmark = pytest.mark.host

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/live_harness.py"
FINGERPRINT = re.compile(r"fingerprint (SHA256:[A-Za-z0-9+/]+)")
UNUSED_IMAGE = "localhost/unused:test"


def run_harness(
    *arguments: str, tmpdir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if tmpdir is not None:
        environment["TMPDIR"] = str(tmpdir)
    return subprocess.run(
        [sys.executable, str(HARNESS), *arguments],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def test_ephemeral_preflight_generates_a_unique_key_and_removes_it(
    tmp_path: Path,
) -> None:
    key_parent = tmp_path / "keys"
    key_parent.mkdir()

    fingerprints: list[str] = []
    for _attempt in range(2):
        completed = run_harness(
            "--mode",
            "ephemeral",
            "--image",
            UNUSED_IMAGE,
            "--preflight-only",
            tmpdir=key_parent,
        )
        assert completed.returncode == 0, completed.stderr
        match = FINGERPRINT.search(completed.stderr)
        assert match is not None, completed.stderr
        fingerprints.append(match.group(1))
        assert "mode ephemeral" in completed.stderr
        assert "key ssh-ed25519" in completed.stderr

    # A fresh key for every run, and nothing of it survives the run.
    assert fingerprints[0] != fingerprints[1]
    assert not any(key_parent.iterdir())


def test_ephemeral_mode_refuses_supplied_key_paths(tmp_path: Path) -> None:
    completed = run_harness(
        "--mode",
        "ephemeral",
        "--image",
        UNUSED_IMAGE,
        "--public-key",
        "/runtime/key.pub",
        "--preflight-only",
        tmpdir=tmp_path,
    )

    assert completed.returncode != 0
    assert "ephemeral mode generates its own key" in completed.stderr


def test_fido_mode_requires_a_hardware_key(tmp_path: Path) -> None:
    completed = run_harness(
        "--mode",
        "fido",
        "--image",
        UNUSED_IMAGE,
        "--preflight-only",
        tmpdir=tmp_path,
    )

    assert completed.returncode != 0
    assert "fido mode requires --public-key" in completed.stderr


def test_fido_mode_rejects_a_standard_key(tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity)],
        check=True,
    )

    completed = run_harness(
        "--mode",
        "fido",
        "--image",
        UNUSED_IMAGE,
        "--public-key",
        f"{identity}.pub",
        "--identity-file",
        str(identity),
        "--preflight-only",
        tmpdir=tmp_path,
    )

    assert completed.returncode != 0
    assert "not a supported OpenSSH FIDO2 key" in completed.stderr


def test_fido_mode_rejects_a_server_container() -> None:
    completed = run_harness(
        "--mode",
        "fido",
        "--image",
        UNUSED_IMAGE,
        "--server-image",
        UNUSED_IMAGE,
        "--preflight-only",
    )

    assert completed.returncode != 0
    assert "hardware key on this host" in completed.stderr


def test_harness_rejects_unknown_arguments() -> None:
    completed = run_harness("--image", UNUSED_IMAGE, "--key-mode", "ephemeral")

    assert completed.returncode != 0
    assert "unrecognized arguments: --key-mode ephemeral" in completed.stderr
    assert "cleanup complete (status 2)" in completed.stderr


def test_harness_terminates_an_active_child_on_signal(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    child_pid = tmp_path / "child.pid"
    network_name = tmp_path / "network.name"
    fake_podman = fake_bin / "podman"
    fake_podman.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "info") exit 0 ;;
  "info --format {{.Host.Security.Rootless}}") printf 'true\\n' ;;
  "image exists "*) exit 0 ;;
  "network create "*)
    printf '%s\\n' "$BASHPID" > "$FAKE_CHILD_PID"
    printf '%s\\n' "${@: -1}" > "$FAKE_NETWORK_NAME"
    trap 'exit 143' TERM
    while true; do sleep 1; done
    ;;
  "network inspect "*) exit 1 ;;
  *) printf 'unexpected fake Podman call: %s\\n' "$*" >&2; exit 99 ;;
esac
""",
        encoding="utf-8",
    )
    fake_podman.chmod(0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "PODMAN": str(fake_podman),
            "FAKE_CHILD_PID": str(child_pid),
            "FAKE_NETWORK_NAME": str(network_name),
            "REMOTE_SSH_MCP_LIVE_TARGET_CONFINE": "--cap-drop=ALL",
            "REMOTE_SSH_MCP_LIVE_SERVER_CONFINE": "--cap-drop=ALL",
            "TMPDIR": str(tmp_path),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(HARNESS),
            "--mode",
            "ephemeral",
            "--image",
            UNUSED_IMAGE,
            "--server-image",
            UNUSED_IMAGE,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not child_pid.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    f"harness exited before the signal: {stdout=} {stderr=}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("harness did not start the blocking child")
            time.sleep(0.02)

        process.send_signal(signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=8)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 143
    assert "cleanup complete (status 143)" in stderr
    pid = int(child_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not list(tmp_path.glob("remote-ssh-mcp-live-key.*"))
