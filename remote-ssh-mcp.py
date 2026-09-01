#!/usr/bin/env python3
"""Executable entry point for the remote SSH MCP server."""

from pathlib import Path

from remote_ssh_mcp.cli import main

if __name__ == "__main__":
    raise SystemExit(main(repository_root=Path(__file__).resolve().parent))
