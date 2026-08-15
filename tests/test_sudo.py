from __future__ import annotations

import inspect
import shlex
import sys
from pathlib import Path

import pytest

from remote_ssh_mcp.commands import CommandRunner
from remote_ssh_mcp.config import RuntimeConfig
from remote_ssh_mcp.errors import RemoteMCPError
from remote_ssh_mcp.local_paths import LocalPathPolicy
from remote_ssh_mcp.sudo import SUDO_REMOTE_PROGRAM, SudoRunner

FAKE_SUDO = r"""#!{python}
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log = os.environ.get("FAKE_SUDO_ARGV_LOG")
if log:
    Path(log).write_text("\n".join(args), encoding="utf-8")

mode = os.environ.get("FAKE_SUDO_MODE", "success")
if mode == "password":
    print("sudo: a password is required", file=sys.stderr)
    raise SystemExit(1)
if mode == "denied":
    print("user is not allowed to execute the requested command", file=sys.stderr)
    raise SystemExit(1)
if mode == "missing":
    print("sudo: command not found", file=sys.stderr)
    raise SystemExit(127)
if mode == "unknown":
    print("sudo: policy plugin returned an unspecified error", file=sys.stderr)
    raise SystemExit(1)

os.execv("/bin/bash", ["/bin/bash", "--noprofile", "--norc", "-s"])
"""


class LocalShellMaster:
    async def ensure_ready(self) -> None:
        return None

    def command_argv(self, remote_program: str) -> list[str]:
        return shlex.split(remote_program)


@pytest.fixture
def sudo_stack(
    runtime_config: RuntimeConfig, tmp_path: Path
) -> tuple[SudoRunner, Path]:
    fake = tmp_path / "fake-sudo"
    fake.write_text(FAKE_SUDO.format(python=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    paths = LocalPathPolicy(runtime_config.local_root)
    paths.initialize()
    runner = CommandRunner(runtime_config, LocalShellMaster(), paths)  # type: ignore[arg-type]
    remote_program = shlex.join(
        [
            str(fake),
            "-n",
            "-k",
            "--",
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-s",
        ]
    )
    return SudoRunner(runner, remote_program=remote_program), fake


def test_default_invocation_is_noninteractive_and_ignores_cache() -> None:
    assert shlex.split(SUDO_REMOTE_PROGRAM) == [
        "env",
        "LC_ALL=C",
        "sudo",
        "-n",
        "-k",
        "--",
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-s",
    ]
    assert "password" not in inspect.signature(SudoRunner.execute).parameters


@pytest.mark.asyncio
async def test_passwordless_sudo_runs_and_strips_marker(
    sudo_stack: tuple[SudoRunner, Path],
) -> None:
    sudo, _fake = sudo_stack

    result = await sudo.execute("printf root-output; printf root-error >&2")

    assert result.exit_code == 0
    assert result.stdout.raw == b"root-output"
    assert result.stderr.raw == b"root-error"
    assert b"REMOTE_SSH_MCP_SUDO_STARTED" not in result.stderr.raw


@pytest.mark.asyncio
async def test_privileged_command_failure_preserves_exit_code(
    sudo_stack: tuple[SudoRunner, Path],
) -> None:
    sudo, _fake = sudo_stack

    result = await sudo.execute("printf failed >&2; exit 42")

    assert result.exit_code == 42
    assert result.stderr.raw == b"failed"


@pytest.mark.asyncio
async def test_password_requirement_fails_even_with_cached_timestamp(
    sudo_stack: tuple[SudoRunner, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sudo, _fake = sudo_stack
    argv_log = tmp_path / "sudo-argv"
    monkeypatch.setenv("FAKE_SUDO_MODE", "password")
    monkeypatch.setenv("FAKE_SUDO_CACHED_TIMESTAMP", "valid")
    monkeypatch.setenv("FAKE_SUDO_ARGV_LOG", str(argv_log))

    with pytest.raises(RemoteMCPError) as raised:
        await sudo.execute("id")

    assert raised.value.code == "sudo_password_required"
    assert argv_log.read_text(encoding="utf-8").splitlines() == [
        "-n",
        "-k",
        "--",
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-s",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("denied", "sudo_not_allowed"),
        ("missing", "sudo_unavailable"),
        ("unknown", "sudo_refused"),
    ],
)
async def test_sudo_start_failure_classification(
    sudo_stack: tuple[SudoRunner, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    code: str,
) -> None:
    sudo, _fake = sudo_stack
    monkeypatch.setenv("FAKE_SUDO_MODE", mode)

    with pytest.raises(RemoteMCPError) as raised:
        await sudo.execute("id")

    assert raised.value.code == code


@pytest.mark.asyncio
async def test_sudo_spool_does_not_contain_internal_marker(
    sudo_stack: tuple[SudoRunner, Path],
) -> None:
    sudo, _fake = sudo_stack

    result = await sudo.execute("head -c 70000 /dev/zero >&2", spool_output=True)

    assert result.stderr.spool_path is not None
    spool = sudo.runner.paths.root / result.stderr.spool_path
    assert spool.read_bytes() == b"\x00" * 70000
    assert result.stderr.total_bytes == 70000
