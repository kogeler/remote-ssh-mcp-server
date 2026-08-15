"""Command-line parsing for the local remote SSH MCP process."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from . import __version__
from .config import RuntimeConfig
from .errors import RemoteMCPError
from .server import run_stdio

DEFAULT_CONNECT_TIMEOUT = 120.0
DEFAULT_COMMAND_TIMEOUT = 120.0
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
DEFAULT_MAX_TRANSFERS = 2


def _find_remote_error(error: BaseException) -> RemoteMCPError | None:
    if isinstance(error, RemoteMCPError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            found = _find_remote_error(nested)
            if found is not None:
                return found
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remote-ssh-mcp",
        description=(
            "Run a disconnected local STDIO MCP server with explicit SSH lifecycle tools."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--local-root",
        required=True,
        help="absolute local directory containing all allowed local file operations",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        metavar="SECONDS",
        help=f"master startup deadline (default: {DEFAULT_CONNECT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=DEFAULT_COMMAND_TIMEOUT,
        metavar="SECONDS",
        help=f"default remote command deadline (default: {DEFAULT_COMMAND_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=DEFAULT_MAX_OUTPUT_BYTES,
        metavar="BYTES",
        help=(
            "maximum captured bytes per command stream "
            f"(default: {DEFAULT_MAX_OUTPUT_BYTES})"
        ),
    )
    parser.add_argument(
        "--max-transfers",
        type=int,
        default=DEFAULT_MAX_TRANSFERS,
        metavar="COUNT",
        help=f"maximum concurrent background transfers (default: {DEFAULT_MAX_TRANSFERS})",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="diagnostic verbosity on stderr (default: INFO)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = RuntimeConfig.from_namespace(args)
    except RemoteMCPError as error:
        parser.error(error.message)

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(run_stdio(config))
    except KeyboardInterrupt:
        return 130
    except RemoteMCPError as error:
        print(f"remote-ssh-mcp: {error.code}: {error.message}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - CLI errors must stay concise.
        remote_error = _find_remote_error(error)
        if remote_error is not None:
            print(
                f"remote-ssh-mcp: {remote_error.code}: {remote_error.message}",
                file=sys.stderr,
            )
            return 1
        print(
            f"remote-ssh-mcp: server_failed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0
