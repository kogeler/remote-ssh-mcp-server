from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

FAKE_PYTHON = r"""#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == -m && ${2:-} == venv ]]; then
    target=${3:?missing venv target}
    mkdir -p "$target/bin"
    cp -- "$0" "$target/bin/python"
    printf '%s\n' ':' > "$target/bin/activate"
    exit 0
fi

if [[ ${1:-} == -m && ${2:-} == pip && ${3:-} == install ]]; then
    printf '%s\n' install >> "${FAKE_PIP_LOG:?missing fake pip log}"
    if [[ ${FAKE_PIP_FAIL:-0} == 1 ]]; then
        exit 42
    fi
    exit 0
fi

printf 'unexpected fake Python invocation:' >&2
printf ' <%s>' "$@" >&2
printf '\n' >&2
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
    shutil.copy2(source, launcher)
    write_executable(tool / "remote-ssh-mcp.py", FAKE_ENTRY_POINT)
    (tool / "requirements.txt").write_text("dependency==1\n", encoding="utf-8")
    write_executable(fake_bin / "python3", FAKE_PYTHON)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "FAKE_PIP_LOG": str(tmp_path / "pip.log"),
        }
    )
    return launcher, tool, environment


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


def test_launcher_bootstraps_forwards_arguments_and_skips_matching_freeze(
    tmp_path: Path,
) -> None:
    launcher, tool, environment = isolated_launcher(tmp_path)
    unrelated_cwd = tmp_path / "unrelated cwd"
    unrelated_cwd.mkdir()

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
    assert "Creating virtual environment" in first.stderr
    assert "Installing dependencies" in first.stderr
    assert (tool / "venv/.requirements.txt").read_bytes() == (
        tool / "requirements.txt"
    ).read_bytes()

    second = run_launcher(launcher, tool, environment, "--help")
    assert second.returncode == 0, second.stderr
    assert "Installing dependencies" not in second.stderr
    assert (tmp_path / "pip.log").read_text(encoding="utf-8").splitlines() == [
        "install"
    ]


def test_failed_dependency_refresh_is_retried_on_next_launch(tmp_path: Path) -> None:
    launcher, tool, environment = isolated_launcher(tmp_path)
    initial = run_launcher(launcher, tmp_path, environment)
    assert initial.returncode == 0, initial.stderr
    old_marker = (tool / "venv/.requirements.txt").read_bytes()

    (tool / "requirements.txt").write_text("dependency==2\n", encoding="utf-8")
    failing_environment = {**environment, "FAKE_PIP_FAIL": "1"}
    failed = run_launcher(launcher, tmp_path, failing_environment)
    assert failed.returncode != 0
    assert "will be retried on the next run" in failed.stderr
    assert (tool / "venv/.requirements.txt").read_bytes() == old_marker

    retried = run_launcher(launcher, tmp_path, environment)
    assert retried.returncode == 0, retried.stderr
    assert (tool / "venv/.requirements.txt").read_bytes() == b"dependency==2\n"
    assert (tmp_path / "pip.log").read_text(encoding="utf-8").splitlines() == [
        "install",
        "install",
        "install",
    ]


def test_public_launcher_help_works_from_unrelated_directory(tmp_path: Path) -> None:
    launcher = Path(__file__).resolve().parents[1] / "remote-ssh-mcp"

    completed = subprocess.run(
        [launcher, "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--local-root" in completed.stdout
    assert "--target" not in completed.stdout
