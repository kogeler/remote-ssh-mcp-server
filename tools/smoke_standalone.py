# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Exercise a standalone executable outside the source tree."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StandaloneSmokeError(RuntimeError):
    """A standalone command behavior differs from the public contract."""


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    expected: int,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        input=stdin,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != expected:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise StandaloneSmokeError(
            f"command returned {completed.returncode}, expected {expected}: {detail}"
        )
    return completed


def smoke(artifact: Path, *, root: Path) -> None:
    """Run public standalone routes with hostile cwd and import state."""
    artifact = artifact.resolve()
    if not artifact.is_file() or not os.access(artifact, os.X_OK):
        raise StandaloneSmokeError(f"standalone is not executable: {artifact}")
    version = (root / ".version").read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory(prefix="remote-ssh-mcp-standalone-smoke-") as raw:
        temporary = Path(raw)
        install = temporary / "install"
        hostile = temporary / "hostile"
        install.mkdir()
        hostile.mkdir()
        executable = install / artifact.name
        shutil.copy2(artifact, executable)
        executable.chmod(0o755)
        (hostile / "remote_ssh_mcp.py").write_text(
            "raise RuntimeError('hostile module imported')\n", encoding="utf-8"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONHASHSEED": "0",
                "PYTHONPATH": str(hostile),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        reported = _run(
            [str(executable), "--version"],
            cwd=hostile,
            environment=environment,
            expected=0,
        )
        if reported.stdout.strip() != f"remote-ssh-mcp {version}":
            raise StandaloneSmokeError("standalone reports the wrong version")
        help_output = _run(
            [str(executable), "--help"],
            cwd=hostile,
            environment=environment,
            expected=0,
        )
        for option in (
            "--connect-timeout",
            "--command-timeout",
            "--max-output-bytes",
            "--max-transfers",
        ):
            if option not in help_output.stdout:
                raise StandaloneSmokeError(f"standalone help is missing {option}")
        invalid = _run(
            [str(executable), "--connect-timeout", "0"],
            cwd=hostile,
            environment=environment,
            expected=2,
        )
        if "connect timeout must be between" not in invalid.stderr:
            raise StandaloneSmokeError("standalone validation differs")

        completed = _run(
            [str(executable)],
            cwd=hostile,
            environment=environment,
            expected=0,
            stdin="",
        )
        if completed.stdout or completed.stderr:
            raise StandaloneSmokeError("clean EOF produced unexpected output")
        internal = install / ".remote-ssh-mcp"
        if internal.stat().st_mode & 0o777 != 0o700:
            raise StandaloneSmokeError("standalone local state is not private")
        for name in ("spool", "partials"):
            path = internal / name
            if path.stat().st_mode & 0o777 != 0o700:
                raise StandaloneSmokeError(f"standalone {name} state is not private")
        if (hostile / ".remote-ssh-mcp").exists():
            raise StandaloneSmokeError("standalone used its current directory as root")


def main() -> int:
    """Smoke one generated executable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    arguments = parser.parse_args()
    try:
        smoke(arguments.artifact, root=arguments.root.resolve())
    except (OSError, StandaloneSmokeError, subprocess.SubprocessError) as error:
        print(f"standalone smoke failed: {error}", file=sys.stderr)
        return 1
    print(f"Standalone smoke passed: {arguments.artifact.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
