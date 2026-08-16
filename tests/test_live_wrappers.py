from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

# The harness drives Podman on the machine, which a container cannot do.
pytestmark = pytest.mark.host

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/live-target.sh"
FINGERPRINT = re.compile(r"fingerprint (SHA256:[A-Za-z0-9+/]+)")
UNUSED_IMAGE = "localhost/unused:test"


def run_harness(
    *arguments: str, tmpdir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if tmpdir is not None:
        environment["TMPDIR"] = str(tmpdir)
    return subprocess.run(
        [str(HARNESS), *arguments],
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


def test_harness_rejects_unknown_arguments() -> None:
    completed = run_harness("--key-mode", "ephemeral")

    assert completed.returncode != 0
    assert "unknown argument: --key-mode" in completed.stderr
