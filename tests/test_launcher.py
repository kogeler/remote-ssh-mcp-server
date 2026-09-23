from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
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


ROOT = Path(__file__).resolve().parents[1]
FIRST_MACHINE_ID = "0123456789abcdef" * 2
SECOND_MACHINE_ID = "fedcba9876543210" * 2


def isolated_launcher(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    source = ROOT / "remote-ssh-mcp"
    repository = tmp_path / "repository"
    tool = repository
    fake_bin = tmp_path / "fake-bin"
    repository.mkdir()
    fake_bin.mkdir()
    launcher = repository / "remote-ssh-mcp"
    launcher.write_bytes(source.read_bytes())
    launcher.chmod(source.stat().st_mode & 0o777)
    set_machine_id(tmp_path, FIRST_MACHINE_ID)
    package = repository / "remote_ssh_mcp"
    package.mkdir()
    selector = (ROOT / "remote_ssh_mcp/machine.py").read_text(encoding="utf-8")
    copied = selector.replace(
        'MACHINE_ID_FILE = Path("/etc/machine-id")',
        f"MACHINE_ID_FILE = Path({str(tmp_path / 'machine-id')!r})",
    )
    assert copied != selector
    (package / "machine.py").write_text(copied, encoding="utf-8")
    # The directly executed selector must not import the package or dependencies.
    (package / "__init__.py").write_text(
        "raise AssertionError('package imported before selecting venv')\n",
        encoding="utf-8",
    )
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


def set_machine_id(tmp_path: Path, value: str) -> None:
    (tmp_path / "machine-id").write_text(value + "\n", encoding="ascii")


def selected_root(tool: Path) -> Path:
    """Ask the same selector the launcher uses for the relative venv root."""
    completed = subprocess.run(
        ["/usr/bin/python3", "-I", str(tool / "remote_ssh_mcp/machine.py")],
        capture_output=True,
        check=True,
        text=True,
    )
    return Path(completed.stdout.strip())


def runtime_dir(tool: Path) -> Path:
    return tool / selected_root(tool) / "venv-runtime"


def install_fake_runtime(tool: Path) -> None:
    runtime = runtime_dir(tool)
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
    assert not runtime_dir(tool).exists()

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
    marker = runtime_dir(tool) / ".requirements.txt"
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
    marker = runtime_dir(tool) / ".version"
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
    assert (runtime_dir(tool) / ".requirements.txt").is_file()
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
    runtime = runtime_dir(source)
    assert (runtime / ".requirements.txt").read_bytes() == (
        source / "requirements.txt"
    ).read_bytes()
    assert (runtime / ".version").read_bytes() == (source / ".version").read_bytes()

    pip_probe = subprocess.run(
        [
            runtime / "bin/python",
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


def test_launcher_probe_binds_ssh_wrapper_to_the_runtime_lock(tmp_path: Path) -> None:
    """The import probe reads the locked version instead of a literal."""
    source = Path(__file__).resolve().parents[1]
    launcher = (source / "remote-ssh-mcp").read_text(encoding="utf-8")
    match = re.search(r"-I -c \\\n    '([^']+)'", launcher)
    assert match is not None
    probe = match.group(1)
    assert 'version("ssh-wrapper") == "' not in probe
    lock = (source / "requirements.txt").read_text(encoding="utf-8")

    def run_probe(lock_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-c", probe, str(source / ".version"), lock_path],
            text=True,
            capture_output=True,
            check=False,
        )

    accepted = run_probe(source / "requirements.txt")
    assert accepted.returncode == 0, accepted.stderr

    stale = tmp_path / "requirements.txt"
    stale.write_text(
        re.sub(r"^ssh-wrapper==\S+", "ssh-wrapper==0.0.0", lock, flags=re.MULTILINE),
        encoding="utf-8",
    )
    rejected = run_probe(stale)
    assert rejected.returncode != 0


def test_shared_checkout_selects_only_this_machine_and_keeps_legacy_env(
    tmp_path: Path,
) -> None:
    launcher, tool, environment = isolated_launcher(tmp_path)
    install_fake_runtime(tool)
    first = runtime_dir(tool)
    original_lock = (first / ".requirements.txt").read_bytes()
    legacy = tool / "venv-runtime/bin/python"
    legacy.parent.mkdir(parents=True)
    write_executable(legacy, FORBIDDEN_SYSTEM_PYTHON)
    assert run_launcher(launcher, tmp_path, environment).returncode == 0

    set_machine_id(tmp_path, SECOND_MACHINE_ID)
    second = runtime_dir(tool)
    assert first != second
    missing = run_launcher(launcher, tmp_path, environment)
    assert missing.returncode == 1
    assert "Runtime environment is not installed" in missing.stderr
    install_fake_runtime(tool)
    assert run_launcher(launcher, tmp_path, environment).returncode == 0

    set_machine_id(tmp_path, FIRST_MACHINE_ID)
    assert run_launcher(launcher, tmp_path, environment).returncode == 0
    assert (first / ".requirements.txt").read_bytes() == original_lock
    assert (second / "bin/python").is_file()
    assert legacy.read_text(encoding="utf-8") == FORBIDDEN_SYSTEM_PYTHON
    assert not (tmp_path / "python.log").exists()


def run_make(tool: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", "-f", str(ROOT / "Makefile"), *arguments],
        cwd=tool,
        text=True,
        capture_output=True,
        check=False,
    )


ROLES = ("RUNTIME", "DEV", "LINT", "STANDALONE", "DOCS")


def test_make_and_launcher_share_environment_paths_and_fail_closed(
    tmp_path: Path,
) -> None:
    launcher, tool, environment = isolated_launcher(tmp_path)
    install_fake_runtime(tool)
    shutil.copytree(ROOT / "make", tool / "make")
    root = selected_root(tool)
    report = "print-venvs:\n\t@printf '%s\\n' " + " ".join(
        f"'$({role}_VENV)'" for role in ROLES
    )

    selected = run_make(tool, f"--eval={report}", "print-venvs")
    assert selected.returncode == 0, selected.stderr
    assert selected.stdout.splitlines() == [
        str(root / f"venv-{role.lower()}") for role in ROLES
    ]
    prepared = run_make(tool, "-n", "runtime-venv")
    assert prepared.returncode == 0, prepared.stderr
    assert f"{root}/venv-runtime/bin/python" in prepared.stdout
    audited = run_make(tool, "-n", "licenses")
    assert audited.returncode == 0, audited.stderr
    assert f"license_policy.py --venv-root '{root}'" in audited.stdout
    cleanup = run_make(tool, "-n", "clean")
    assert cleanup.returncode == 0, cleanup.stderr
    assert all(f"'{root}/venv-{role.lower()}'" in cleanup.stdout for role in ROLES)
    assert "'.venvs'" not in cleanup.stdout
    assert "'venv-runtime'" not in cleanup.stdout

    set_machine_id(tmp_path, "uninitialized")
    refused = run_make(tool, f"--eval={report}", "print-venvs")
    assert refused.returncode != 0
    assert not refused.stdout
    assert "Cannot select machine-specific environments" in refused.stderr
    refused = run_launcher(launcher, tmp_path, environment)
    assert refused.returncode == 1
    assert not refused.stdout
    assert "invalid or uninitialized" in refused.stderr
    assert (tool / root / "venv-runtime/bin/python").is_file()
    assert not (tmp_path / "python.log").exists()


def test_make_clean_preserves_other_machine_and_legacy_environments(
    tmp_path: Path,
) -> None:
    _launcher, tool, _environment = isolated_launcher(tmp_path)
    shutil.copytree(ROOT / "make", tool / "make")
    for directory in ("tests", "tools", "doc/site", ".github/scripts"):
        (tool / directory).mkdir(parents=True)
    first = selected_root(tool)
    for role in ROLES:
        directory = tool / first / f"venv-{role.lower()}"
        directory.mkdir(parents=True)
        (directory / "dependency.py").write_text("owned\n", encoding="utf-8")
    set_machine_id(tmp_path, SECOND_MACHINE_ID)
    second = selected_root(tool)
    for role in ROLES:
        for base in (tool / second, tool):
            directory = base / f"venv-{role.lower()}"
            directory.mkdir(parents=True)
            (directory / "dependency.py").write_text("keep\n", encoding="utf-8")
    set_machine_id(tmp_path, FIRST_MACHINE_ID)

    completed = run_make(tool, "clean")

    assert completed.returncode == 0, completed.stderr
    for role in ROLES:
        assert not (tool / first / f"venv-{role.lower()}").exists()
        for base in (tool / second, tool):
            assert (base / f"venv-{role.lower()}/dependency.py").read_text(
                encoding="utf-8"
            ) == "keep\n"
