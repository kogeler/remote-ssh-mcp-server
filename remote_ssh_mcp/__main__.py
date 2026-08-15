"""Module entry point for ``python -m remote_ssh_mcp``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
