from __future__ import annotations

import pytest

from remote_ssh_mcp import __version__
from remote_ssh_mcp.cli import _find_remote_error, build_parser
from remote_ssh_mcp.errors import RemoteMCPError


def test_help_exits_without_required_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--local-root" in output
    assert "--target" not in output


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["--version"])

    assert raised.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_parser_preserves_local_root_without_target() -> None:
    args = build_parser().parse_args(["--local-root", "/tmp/work tree"])

    assert args.local_root == "/tmp/work tree"
    assert not hasattr(args, "target")


def test_expected_startup_error_is_found_inside_exception_group() -> None:
    expected = RemoteMCPError("connection_start_failed", "master failed")
    grouped = ExceptionGroup("transport", [RuntimeError("noise"), expected])

    assert _find_remote_error(grouped) is expected
