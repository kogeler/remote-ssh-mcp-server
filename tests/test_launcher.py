from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from remote_ssh_mcp import __version__

FAKE_RUNTIME_PYTHON = r"""#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} == -I ]]
shift
if [[ ${1:-} == -c ]]; then
    [[ ${FAKE_RUNTIME_INVALID:-0} != 1 ]]
    exit
fi
[[ ${1:-} == -m ]]
[[ ${2:-} == remote_ssh_mcp ]]
shift 2
exec "${FAKE_ENTRY_POINT_PATH:?missing fake entry point}" "$@"
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
    fake_entry_point = tmp_path / "fake-entry-point"
    write_executable(fake_entry_point, FAKE_ENTRY_POINT)
    (tool / "requirements.txt").write_text("dependency==1\n", encoding="utf-8")
    (tool / ".version").write_text(f"{__version__}\n", encoding="utf-8")
    write_executable(fake_bin / "python3", FORBIDDEN_SYSTEM_PYTHON)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "FAKE_ENTRY_POINT_PATH": str(fake_entry_point),
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
    (runtime / ".version").write_bytes((tool / ".version").read_bytes())


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


def test_launcher_rejects_stale_project_version_without_modifying_it(
    tmp_path: Path,
) -> None:
    launcher, tool, environment = isolated_launcher(tmp_path)
    install_fake_runtime(tool)
    marker = tool / "venv-runtime/.version"
    old_marker = marker.read_bytes()

    (tool / ".version").write_text("999.0.0\n", encoding="utf-8")
    failed = run_launcher(launcher, tmp_path, environment)

    assert failed.returncode != 0
    assert "Runtime environment is stale" in failed.stderr
    assert marker.read_bytes() == old_marker
    assert not (tmp_path / "python.log").exists()


def test_launcher_rejects_an_invalid_runtime_without_repairing_it(
    tmp_path: Path,
) -> None:
    """An interpreter upgrade cannot silently orphan installed packages."""
    launcher, tool, environment = isolated_launcher(tmp_path)
    install_fake_runtime(tool)
    environment["FAKE_RUNTIME_INVALID"] = "1"

    failed = run_launcher(launcher, tmp_path, environment)

    assert failed.returncode != 0
    assert "Runtime environment is invalid" in failed.stderr
    assert "run: make runtime-venv" in failed.stderr
    assert (tool / "venv-runtime/.requirements.txt").is_file()
    assert not (tmp_path / "python.log").exists()


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
    assert "--connect-timeout" in completed.stdout
    assert "--target" not in completed.stdout
    assert (source / "venv-runtime/.requirements.txt").read_bytes() == (
        source / "requirements.txt"
    ).read_bytes()
    assert (source / "venv-runtime/.version").read_bytes() == (
        source / ".version"
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


@pytest.mark.host
def test_public_launcher_ignores_hostile_pythonpath(tmp_path: Path) -> None:
    """Ambient import paths cannot replace the prepared runtime packages."""
    source = Path(__file__).resolve().parents[1]
    hostile = tmp_path / "hostile"
    (hostile / "remote_ssh_mcp").mkdir(parents=True)
    (hostile / "remote_ssh_mcp/__init__.py").write_text(
        'raise RuntimeError("ambient remote_ssh_mcp imported")\n', encoding="utf-8"
    )
    (hostile / "ssh_wrapper").mkdir()
    (hostile / "ssh_wrapper/__init__.py").write_text(
        'raise RuntimeError("ambient ssh_wrapper imported")\n', encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(hostile)

    completed = subprocess.run(
        [source / "remote-ssh-mcp", "--connect-timeout", "0"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2, completed.stderr
    assert "connect timeout must be between" in completed.stderr
    assert "ambient" not in completed.stderr
