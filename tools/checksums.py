# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Write and verify the exact SHA-256 inventory for standalone releases."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

SHA256 = re.compile(r"[0-9a-f]{64}")


class ChecksumError(ValueError):
    """A checksum inventory is missing, malformed, or unexpected."""


def expected_names() -> set[str]:
    """Return the complete standalone GitHub Release artifact inventory."""
    return {"remote-ssh-mcp-linux-amd64", "remote-ssh-mcp-linux-arm64"}


def write(directory: Path) -> Path:
    """Write checksums for exactly the expected standalone artifacts."""
    names = expected_names()
    actual = {
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.name != "SHA256SUMS.txt"
    }
    if actual != names:
        raise ChecksumError(f"release inventory {sorted(actual)} != {sorted(names)}")
    output = directory / "SHA256SUMS.txt"
    output.write_text(
        "".join(
            f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n"
            for name in sorted(names)
        ),
        encoding="ascii",
    )
    output.chmod(0o644)
    return output


def verify(directory: Path) -> None:
    """Verify the checksum file and complete local release inventory."""
    path = directory / "SHA256SUMS.txt"
    expected = expected_names()
    actual = {
        candidate.name for candidate in directory.iterdir() if candidate.is_file()
    }
    if actual != expected | {path.name}:
        raise ChecksumError(
            f"release inventory {sorted(actual)} != {sorted(expected | {path.name})}"
        )
    parsed: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or SHA256.fullmatch(digest) is None or not name:
            raise ChecksumError(f"malformed checksum line: {line!r}")
        if name in parsed:
            raise ChecksumError(f"duplicate checksum entry: {name}")
        parsed[name] = digest
    if set(parsed) != expected:
        raise ChecksumError("checksum entries differ from the release inventory")
    for name, digest in parsed.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ChecksumError(f"checksum mismatch for {name}")


def main() -> int:
    """Generate and verify the standalone release inventory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        output = write(arguments.directory.resolve())
        verify(arguments.directory.resolve())
    except (ChecksumError, OSError) as error:
        print(f"checksum error: {error}", file=sys.stderr)
        return 1
    print(output.read_text(encoding="ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
