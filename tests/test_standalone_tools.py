# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Focused evidence for standalone build and release helpers."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from tools.build_standalone import (
    StandaloneBuildError,
    source_digest,
    standalone_architecture,
    standalone_name,
)
from tools.checksums import ChecksumError, expected_names, verify, write
from tools.standalone_entry import standalone_root
from tools.verify_standalone import StandaloneError
from tools.verify_standalone import verify as verify_standalone

ROOT = Path(__file__).resolve().parents[1]
EPOCH = 315_532_800


@pytest.mark.parametrize(
    ("machine", "expected"),
    (
        ("x86_64", "amd64"),
        ("AMD64", "amd64"),
        ("aarch64", "arm64"),
        ("ARM64", "arm64"),
    ),
)
def test_standalone_architecture_and_linux_names(machine: str, expected: str) -> None:
    """Public binary names derive only from supported native Linux machines."""
    assert standalone_architecture(machine) == expected
    assert standalone_name("linux", machine) == f"remote-ssh-mcp-linux-{expected}"


def test_standalone_rejects_foreign_platforms_and_architectures() -> None:
    """Cross-platform or unknown binary identities fail closed."""
    with pytest.raises(StandaloneBuildError, match="unsupported standalone target"):
        standalone_name("win32", "x86_64")
    with pytest.raises(
        StandaloneBuildError, match="unsupported standalone architecture"
    ):
        standalone_architecture("mips64")


def test_entry_uses_executable_directory_not_cwd_or_bundle_directory(
    tmp_path: Path,
) -> None:
    """A one-file extraction directory cannot become the local file boundary."""
    install = tmp_path / "install"
    install.mkdir()
    executable = install / "remote-ssh-mcp-linux-amd64"
    executable.write_bytes(b"executable")

    assert standalone_root(str(executable)) == install.resolve()


def test_checksum_inventory_is_exact_and_self_verifying(tmp_path: Path) -> None:
    """Release checksums bind exactly two architecture-specific executables."""
    for name in expected_names():
        (tmp_path / name).write_bytes(name.encode())
    output = write(tmp_path)
    verify(tmp_path)
    first = output.read_bytes()
    write(tmp_path)
    assert output.read_bytes() == first
    (tmp_path / "unexpected").write_bytes(b"unexpected")
    with pytest.raises(ChecksumError, match="inventory"):
        write(tmp_path)


def _fake_standalone(tmp_path: Path, architecture: str = "amd64") -> tuple[Path, Path]:
    machine = {"amd64": 62, "arm64": 183}[architecture]
    data = bytearray(64)
    data[:6] = b"\x7fELF\x02\x01"
    data[18:20] = struct.pack("<H", machine)
    artifact = tmp_path / f"remote-ssh-mcp-linux-{architecture}"
    artifact.write_bytes(data)
    artifact.chmod(0o755)
    provenance = tmp_path / "provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "schema": 1,
                "artifact": {
                    "name": artifact.name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                },
                "architecture": architecture,
                "python": "3.14.4",
                "pyinstaller": "6.22.2",
                "source_date_epoch": EPOCH,
                "source_sha256": source_digest(ROOT),
                "standalone_lock_sha256": hashlib.sha256(
                    (ROOT / "requirements-standalone.txt").read_bytes()
                ).hexdigest(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact, provenance


def test_standalone_verifier_binds_elf_and_provenance(tmp_path: Path) -> None:
    """ELF identity, source, lock, and artifact bytes are one contract."""
    artifact, provenance = _fake_standalone(tmp_path)
    verify_standalone(
        artifact,
        provenance=provenance,
        root=ROOT,
        architecture="amd64",
        epoch=EPOCH,
    )
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    with pytest.raises(StandaloneError, match="provenance artifact"):
        verify_standalone(
            artifact,
            provenance=provenance,
            root=ROOT,
            architecture="amd64",
            epoch=EPOCH,
        )
