# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Stable machine-scoped environment namespaces, usable before any venv exists."""

from __future__ import annotations

import hmac
import os
import re
import sys
from pathlib import Path

MACHINE_ID_FILE = Path("/etc/machine-id")
VENV_NAMESPACE_DIRECTORY = ".venvs"


class MachineIdentityError(RuntimeError):
    """No valid OS identity is available for selecting a private environment."""


def environment_key() -> str:
    """Derive an application-specific key without publishing the raw machine ID."""
    try:
        with MACHINE_ID_FILE.open("rb") as stream:
            value = stream.read(34)
    except OSError as error:
        raise MachineIdentityError(
            "cannot read /etc/machine-id; a valid OS machine ID is required"
        ) from error
    if re.fullmatch(rb"[0-9a-f]{32}\n?", value) is None or int(value, 16) == 0:
        raise MachineIdentityError(
            "/etc/machine-id is invalid or uninitialized; "
            "no shared venv fallback is allowed"
        )
    identity = bytes.fromhex(value.decode("ascii").strip())
    return hmac.digest(
        b"remote-ssh-mcp/venv-namespace/v1",
        identity + b"\0" + str(os.getuid()).encode("ascii"),
        "sha256",
    ).hex()


def repository_venv_root() -> Path:
    """Return the same relative root for Make and the repository launcher."""
    return Path(VENV_NAMESPACE_DIRECTORY) / environment_key()


def main() -> int:
    """Run directly with isolated system Python, without importing the package."""
    try:
        print(repository_venv_root())
    except MachineIdentityError as error:
        print(f"remote-ssh-mcp: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
