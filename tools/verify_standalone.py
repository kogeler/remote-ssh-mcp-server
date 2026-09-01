# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Verify a native standalone's ELF identity and recorded provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

if __package__:
    from .build_standalone import source_digest
else:
    from build_standalone import source_digest

ROOT = Path(__file__).resolve().parents[1]
ELF_MACHINES = {"amd64": 62, "arm64": 183}


class StandaloneError(ValueError):
    """The executable or provenance record violates the release contract."""


def verify(
    artifact: Path, *, provenance: Path, root: Path, architecture: str, epoch: int
) -> None:
    """Verify one executable without trusting its filename alone."""
    expected_name = f"remote-ssh-mcp-linux-{architecture}"
    if architecture not in ELF_MACHINES:
        raise StandaloneError(f"unsupported architecture: {architecture}")
    if (
        artifact.name != expected_name
        or not artifact.is_file()
        or artifact.is_symlink()
        or artifact.stat().st_size <= 0
    ):
        raise StandaloneError(f"standalone artifact differs: {artifact}")
    if artifact.stat().st_mode & 0o777 != 0o755:
        raise StandaloneError("standalone mode must be 0755")
    data = artifact.read_bytes()
    if len(data) < 20 or data[:6] != b"\x7fELF\x02\x01":
        raise StandaloneError("standalone is not a little-endian 64-bit ELF")
    machine = struct.unpack("<H", data[18:20])[0]
    if machine != ELF_MACHINES[architecture]:
        raise StandaloneError(f"ELF machine {machine} does not match {architecture}")
    forbidden = (os.fsencode(root.resolve()),)
    if any(marker and marker in data for marker in forbidden):
        raise StandaloneError("standalone contains a private or foreign path marker")

    document = json.loads(provenance.read_text(encoding="utf-8"))
    expected_keys = {
        "schema",
        "artifact",
        "architecture",
        "python",
        "pyinstaller",
        "source_date_epoch",
        "source_sha256",
        "standalone_lock_sha256",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise StandaloneError("standalone provenance shape differs")
    expected_artifact = {
        "name": expected_name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    if document["schema"] != 1 or document["artifact"] != expected_artifact:
        raise StandaloneError("standalone provenance artifact differs")
    if (
        document["architecture"] != architecture
        or document["source_date_epoch"] != epoch
    ):
        raise StandaloneError("standalone provenance target differs")
    if document["source_sha256"] != source_digest(root):
        raise StandaloneError("standalone provenance source digest differs")
    lock_digest = hashlib.sha256(
        (root / "requirements-standalone.txt").read_bytes()
    ).hexdigest()
    if document["standalone_lock_sha256"] != lock_digest:
        raise StandaloneError("standalone provenance lock digest differs")
    if not isinstance(document["python"], str) or not isinstance(
        document["pyinstaller"], str
    ):
        raise StandaloneError("standalone tool provenance differs")


def main() -> int:
    """Verify one generated executable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    arguments = parser.parse_args()
    try:
        verify(
            arguments.artifact.resolve(),
            provenance=arguments.provenance.resolve(),
            root=arguments.root.resolve(),
            architecture=arguments.architecture,
            epoch=arguments.epoch,
        )
    except (OSError, json.JSONDecodeError, StandaloneError) as error:
        print(f"standalone verification failed: {error}", file=sys.stderr)
        return 1
    print(f"Verified {arguments.artifact.name} as Linux {arguments.architecture}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
