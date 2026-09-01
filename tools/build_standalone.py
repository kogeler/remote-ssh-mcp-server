# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Build and describe one native Linux one-file Remote SSH MCP executable."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


class StandaloneBuildError(RuntimeError):
    """The standalone build target or output is invalid."""


def standalone_architecture(machine: str = platform.machine()) -> str:
    """Map a native Linux machine identifier to the public artifact name."""
    architecture = ARCHITECTURES.get(machine.casefold())
    if architecture is None:
        raise StandaloneBuildError(f"unsupported standalone architecture: {machine}")
    return architecture


def standalone_name(
    system: str = sys.platform, machine: str = platform.machine()
) -> str:
    """Return the only valid artifact name for a native target."""
    architecture = standalone_architecture(machine)
    if system != "linux":
        raise StandaloneBuildError(
            f"unsupported standalone target: {system}/{architecture}"
        )
    return f"remote-ssh-mcp-linux-{architecture}"


def _remove_owned(path: Path, *, root: Path) -> None:
    """Remove only a generated path contained by the repository root."""
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise StandaloneBuildError(
            f"refusing to remove outside the root: {path}"
        ) from error
    if resolved == root.resolve():
        raise StandaloneBuildError("refusing to remove the repository root")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def source_digest(root: Path) -> str:
    """Hash every source input which can affect the standalone executable."""
    digest = hashlib.sha256()
    paths = [
        root / ".version",
        root / "pyproject.toml",
        root / "requirements-standalone.txt",
        root / "tools/build_standalone.py",
        root / "tools/standalone_entry.py",
        *sorted(
            path
            for path in (root / "remote_ssh_mcp").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ),
    ]
    for path in paths:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build(
    *, root: Path, python: Path, output: Path, epoch: int, expected_arch: str | None
) -> tuple[Path, Path]:
    """Build one native executable and its non-release provenance record."""
    root = root.resolve()
    python = python.absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise StandaloneBuildError(
            f"standalone interpreter is not executable: {python}"
        )
    if sys.platform != "linux":
        raise StandaloneBuildError(
            f"standalone builds require Linux, found {sys.platform}"
        )
    architecture = standalone_architecture()
    if expected_arch is not None and expected_arch != architecture:
        raise StandaloneBuildError(
            f"native architecture {architecture} does not match requested {expected_arch}"
        )
    if epoch < 315_532_800:
        raise StandaloneBuildError("SOURCE_DATE_EPOCH must be at or after 1980-01-01")
    output = output if output.is_absolute() else root / output
    output = output.resolve()
    try:
        output.relative_to(root)
    except ValueError as error:
        raise StandaloneBuildError(
            "standalone output must be inside the root"
        ) from error

    name = standalone_name()
    artifact = output / name
    work = root / "build" / f"pyinstaller-{architecture}"
    provenance = root / ".artifacts" / f"standalone-provenance-{architecture}.json"
    for path in (artifact, work, provenance):
        _remove_owned(path, root=root)
    output.mkdir(parents=True, exist_ok=True)
    (work / "spec").mkdir(parents=True)
    provenance.parent.mkdir(parents=True, exist_ok=True)

    environment = os.environ.copy()
    environment.update(
        {
            "LC_ALL": "C.UTF-8",
            "PYINSTALLER_CONFIG_DIR": str(work / "config"),
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(epoch),
            "TZ": "UTC",
        }
    )
    environment.pop("PYTHONPATH", None)
    command = [
        str(python),
        "-m",
        "PyInstaller",
        str(root / "tools/standalone_entry.py"),
        "--name",
        name,
        "--onefile",
        "--console",
        "--clean",
        "--noconfirm",
        "--noupx",
        "--log-level=WARN",
        "--distpath",
        str(output),
        "--workpath",
        str(work / "work"),
        "--specpath",
        str(work / "spec"),
        "--paths",
        str(root),
        "--collect-submodules",
        "ssh_wrapper",
        "--copy-metadata",
        "mcp",
        "--copy-metadata",
        "pydantic",
        "--copy-metadata",
        "ssh-wrapper",
    ]
    completed = subprocess.run(
        command, cwd=root, env=environment, check=False, text=True, timeout=900
    )
    if completed.returncode != 0:
        raise StandaloneBuildError(
            f"PyInstaller exited with status {completed.returncode}"
        )
    if not artifact.is_file() or artifact.is_symlink() or artifact.stat().st_size <= 0:
        raise StandaloneBuildError(f"PyInstaller did not create {artifact.name}")
    artifact.chmod(0o755)
    document = {
        "schema": 1,
        "artifact": {
            "name": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "size": artifact.stat().st_size,
        },
        "architecture": architecture,
        "python": platform.python_version(),
        "pyinstaller": importlib.metadata.version("pyinstaller"),
        "source_date_epoch": epoch,
        "source_sha256": source_digest(root),
        "standalone_lock_sha256": hashlib.sha256(
            (root / "requirements-standalone.txt").read_bytes()
        ).hexdigest(),
    }
    provenance.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    provenance.chmod(0o644)
    return artifact, provenance


def main() -> int:
    """Build the selected native executable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--expected-architecture", choices=("amd64", "arm64"))
    arguments = parser.parse_args()
    try:
        artifact, provenance = build(
            root=arguments.root,
            python=arguments.python,
            output=arguments.output,
            epoch=arguments.epoch,
            expected_arch=arguments.expected_architecture,
        )
    except (OSError, StandaloneBuildError, subprocess.SubprocessError) as error:
        print(f"standalone build failed: {error}", file=sys.stderr)
        return 1
    print(f"Built {artifact.name}; provenance: {provenance.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
