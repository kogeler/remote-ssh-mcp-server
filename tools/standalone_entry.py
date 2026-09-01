# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""PyInstaller entry point for the standalone Remote SSH MCP executable."""

from __future__ import annotations

import sys
from pathlib import Path

from remote_ssh_mcp.cli import main


def standalone_root(executable: str = sys.executable) -> Path:
    """Use the public executable's directory as the standalone local boundary."""
    return Path(executable).resolve(strict=True).parent


def run() -> int:
    """Run the MCP server with a stable boundary outside PyInstaller's temp tree."""
    return main(repository_root=standalone_root())


if __name__ == "__main__":
    raise SystemExit(run())
