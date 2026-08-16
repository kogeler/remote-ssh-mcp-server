from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

FAKE_RUNTIME_PYTHON = r"""#!/usr/bin/env bash
set -euo pipefail
entry_point=${1:?missing entry point}
shift
exec "$entry_point" "$@"
"""

FORBIDDEN_SYSTEM_PYTHON = r"""#!/usr/bin/env bash
printf '%s\n' invoked >> "${FORBIDDEN_PYTHON_LOG:?missing invocation log}"
exit 99
"""

FAKE_ENTRY_POINT = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'argc=%s\n' "$#"
printf '<%s>\n' "$@"
"""


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def isolated_launcher(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    source = Path(__file__).resolve().parents[1] / "remote-ssh-mcp"
    repository = tmp_path / "repository"
    tool = repository
    fake_bin = tmp_path / "fake-bin"
    repository.mkdir()
    fake_bin.mkdir()

    launcher = repository / "remote-ssh-mcp"
    launcher.write_bytes(source.read_bytes())
    launcher.chmod(source.stat().st_mode & 0o777)
    write_executable(tool / "remote-ssh-mcp.py", FAKE_ENTRY_POINT)
    (tool / "requirements.txt").write_text("dependency==1\n", encoding="utf-8")
    write_executable(fake_bin / "python3", FORBIDDEN_SYSTEM_PYTHON)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "FORBIDDEN_PYTHON_LOG": str(tmp_path / "python.log"),
        }
    )
    return launcher, tool, environment


def install_fake_runtime(tool: Path) -> None:
    runtime = tool / "venv-runtime"
    (runtime / "bin").mkdir(parents=True)
    write_executable(runtime / "bin/python", FAKE_RUNTIME_PYTHON)
    (runtime / ".requirements.txt").write_bytes(
        (tool / "requirements.txt").read_bytes()
    )


def run_launcher(
    launcher: Path,
    cwd: Path,
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [launcher, *arguments],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_launcher_requires_explicit_runtime_and_forwards_arguments(
    tmp_path: Path,
) -> None:
    launcher, tool, environment = isolated_launcher(tmp_path)
    unrelated_cwd = tmp_path / "unrelated cwd"
    unrelated_cwd.mkdir()

    missing = run_launcher(launcher, unrelated_cwd, environment, "--help")
    assert missing.returncode != 0
    assert "run: make runtime-venv" in missing.stderr
    assert not (tmp_path / "python.log").exists()
    assert not (tool / "venv-runtime").exists()

    install_fake_runtime(tool)
    first = run_launcher(
        launcher,
        unrelated_cwd,
        environment,
        "--option",
        "value with spaces",
        "",
    )
    assert first.returncode == 0, first.stderr
    assert first.stdout == "argc=3\n<--option>\n<value with spaces>\n<>\n"
    assert first.stderr == ""

    second = run_launcher(launcher, tool, environment, "--help")
    assert second.returncode == 0, second.stderr
    assert not (tmp_path / "python.log").exists()


def test_launcher_rejects_stale_runtime_without_modifying_it(tmp_path: Path) -> None:
    launcher, tool, environment = isolated_launcher(tmp_path)
    install_fake_runtime(tool)
    marker = tool / "venv-runtime/.requirements.txt"
    old_marker = marker.read_bytes()

    (tool / "requirements.txt").write_text("dependency==2\n", encoding="utf-8")
    failed = run_launcher(launcher, tmp_path, environment)
    assert failed.returncode != 0
    assert "Runtime environment is stale" in failed.stderr
    assert "run: make runtime-venv" in failed.stderr
    assert marker.read_bytes() == old_marker
    assert not (tmp_path / "python.log").exists()

    marker.write_bytes((tool / "requirements.txt").read_bytes())
    current = run_launcher(launcher, tmp_path, environment)
    assert current.returncode == 0, current.stderr


@pytest.mark.host
def test_public_launcher_help_works_from_unrelated_directory(tmp_path: Path) -> None:
    """Prove the prepared runtime works without the repository as cwd."""
    source = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [source / "remote-ssh-mcp", "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--local-root" in completed.stdout
    assert "--target" not in completed.stdout
    assert (source / "venv-runtime/.requirements.txt").read_bytes() == (
        source / "requirements.txt"
    ).read_bytes()

    pip_probe = subprocess.run(
        [
            source / "venv-runtime/bin/python",
            "-c",
            (
                "import importlib.util, sys; "
                "sys.exit(importlib.util.find_spec('pip') is not None)"
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert pip_probe.returncode == 0, pip_probe.stderr
