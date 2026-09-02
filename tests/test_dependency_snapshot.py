# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Regression tests for the exact GitHub dependency snapshot."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SNAPSHOT_SCRIPT = ROOT / ".github" / "scripts" / "dependency_snapshot.py"
LOCKS = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-docs.txt",
    "requirements-lint.txt",
    "requirements-standalone.txt",
)
INPUTS = (
    "requirements.in",
    "requirements-dev.in",
    "requirements-docs.in",
    "requirements-lint.in",
    "requirements-standalone.in",
)


def _run_snapshot(root: Path, output: Path) -> subprocess.CompletedProcess[str]:
    """Run the production helper against a selected repository root."""
    return subprocess.run(
        [
            sys.executable,
            str(SNAPSHOT_SCRIPT),
            "--root",
            str(root),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _copy_inputs(destination: Path) -> None:
    """Copy the package link, five inputs, and five generated locks."""
    shutil.copy2(ROOT / "pyproject.toml", destination / "pyproject.toml")
    for name in INPUTS + LOCKS:
        shutil.copy2(ROOT / name, destination / name)


def _replace_runtime_dependency(path: Path, replacement: str) -> tuple[str, str]:
    """Replace the first direct runtime pin without knowing its current version."""
    lines = path.read_text(encoding="utf-8").splitlines()
    index = next(
        index
        for index, line in enumerate(lines)
        if line and not line.startswith(("#", "-r "))
    )
    current = lines[index]
    assert "==" in current
    name, version = current.split("==", maxsplit=1)
    lines[index] = replacement
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return name, version


def _direct_lint_requirement() -> tuple[str, str]:
    """Read the lint package identity from its sole version authority."""
    requirements = [
        line
        for line in (ROOT / "requirements-lint.in")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("#")
    ]
    assert len(requirements) == 1
    requirement = requirements[0]
    assert "==" in requirement
    name, version = requirement.split("==", maxsplit=1)
    return name, version


def test_snapshot_contains_five_exact_lock_graphs(tmp_path: Path) -> None:
    """Every dependency audience retains distinct lock semantics."""
    output = tmp_path / "nested" / "snapshot.json"
    result = _run_snapshot(ROOT, output)

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) == {"manifests"}
    assert set(payload["manifests"]) == set(LOCKS)

    runtime = payload["manifests"]["requirements.txt"]
    development = payload["manifests"]["requirements-dev.txt"]
    docs = payload["manifests"]["requirements-docs.txt"]
    lint = payload["manifests"]["requirements-lint.txt"]
    standalone = payload["manifests"]["requirements-standalone.txt"]
    for name, manifest in payload["manifests"].items():
        assert manifest["name"] == name
        assert manifest["file"] == {"source_location": name}
        assert manifest["resolved"]

    assert runtime["resolved"]["mcp"]["relationship"] == "direct"
    assert runtime["resolved"]["pydantic"]["relationship"] == "direct"
    assert runtime["resolved"]["annotated-types"]["relationship"] == "indirect"
    assert {item["scope"] for item in runtime["resolved"].values()} == {"runtime"}

    assert development["resolved"]["bandit"]["relationship"] == "direct"
    assert development["resolved"]["mcp"]["relationship"] == "direct"
    assert development["resolved"]["annotated-types"]["relationship"] == "indirect"
    assert {item["scope"] for item in development["resolved"].values()} == {
        "development"
    }
    assert docs["resolved"]["mkdocs-material"]["relationship"] == "direct"
    assert docs["resolved"]["mcp"]["relationship"] == "direct"
    assert {item["scope"] for item in docs["resolved"].values()} == {"development"}
    lint_name, lint_version = _direct_lint_requirement()
    assert lint["resolved"] == {
        lint_name: {
            "package_url": f"pkg:pypi/{lint_name}@{lint_version}",
            "relationship": "direct",
            "scope": "development",
        }
    }
    assert standalone["resolved"]["pyinstaller"]["relationship"] == "direct"
    assert standalone["resolved"]["mcp"]["relationship"] == "direct"
    assert standalone["resolved"]["altgraph"]["relationship"] == "indirect"
    assert {item["scope"] for item in standalone["resolved"].values()} == {
        "development"
    }


def test_snapshot_is_deterministic(tmp_path: Path) -> None:
    """Identical inputs produce byte-identical JSON."""
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    first_result = _run_snapshot(ROOT, first)
    second_result = _run_snapshot(ROOT, second)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert first.read_bytes() == second.read_bytes()


def test_snapshot_rejects_unrecognized_lock_content(tmp_path: Path) -> None:
    """New resolver syntax cannot silently disappear from the graph."""
    _copy_inputs(tmp_path)
    runtime = tmp_path / "requirements.txt"
    runtime.write_text(
        runtime.read_text(encoding="utf-8") + "--index-url example.invalid\n",
        encoding="utf-8",
    )

    result = _run_snapshot(tmp_path, tmp_path / "snapshot.json")

    assert result.returncode == 1
    assert "unsupported lock entry" in result.stderr


def test_snapshot_rejects_missing_direct_dependency(tmp_path: Path) -> None:
    """Every direct dependency must occur in its matching lock."""
    _copy_inputs(tmp_path)
    runtime_input = tmp_path / "requirements.in"
    _replace_runtime_dependency(runtime_input, "missing-package==1.0.0")

    result = _run_snapshot(tmp_path, tmp_path / "snapshot.json")

    assert result.returncode == 1
    assert "direct dependencies missing from lock: missing-package" in result.stderr


def test_snapshot_rejects_direct_version_mismatch(tmp_path: Path) -> None:
    """A direct version cannot disagree with its generated lock."""
    _copy_inputs(tmp_path)
    runtime_input = tmp_path / "requirements.in"
    name, locked_version = _replace_runtime_dependency(
        runtime_input, "placeholder==0.0.0"
    )
    replacement_version = "0.0.1" if locked_version == "0.0.0" else "0.0.0"
    _replace_runtime_dependency(runtime_input, f"{name}=={replacement_version}")

    result = _run_snapshot(tmp_path, tmp_path / "snapshot.json")

    assert result.returncode == 1
    assert f"{name}=={replacement_version} (lock has {locked_version})" in result.stderr


def test_snapshot_rejects_hashless_pin(tmp_path: Path) -> None:
    """The graph cannot be generated from a non-hash lock."""
    _copy_inputs(tmp_path)
    lint = tmp_path / "requirements-lint.txt"
    lint.write_text(
        "# This file is autogenerated by pip-compile\n"
        "fixture-package==1.0.0 \\\n"
        "    # via test\n",
        encoding="utf-8",
    )

    result = _run_snapshot(tmp_path, tmp_path / "snapshot.json")

    assert result.returncode == 1
    assert "fixture-package==1.0.0 has no SHA-256 hash" in result.stderr


def test_snapshot_rejects_duplicate_hash(tmp_path: Path) -> None:
    """Repeated digest lines are rejected as malformed lock content."""
    _copy_inputs(tmp_path)
    digest = "0" * 64
    lint = tmp_path / "requirements-lint.txt"
    lint.write_text(
        "# This file is autogenerated by pip-compile\n"
        "fixture-package==1.0.0 \\\n"
        f"    --hash=sha256:{digest} \\\n"
        f"    --hash=sha256:{digest}\n",
        encoding="utf-8",
    )

    result = _run_snapshot(tmp_path, tmp_path / "snapshot.json")

    assert result.returncode == 1
    assert "duplicate SHA-256 hash" in result.stderr


def test_snapshot_rejects_another_requirements_include(tmp_path: Path) -> None:
    """A derived audience can include only the canonical runtime input."""
    _copy_inputs(tmp_path)
    development_input = tmp_path / "requirements-dev.in"
    development_input.write_text(
        development_input.read_text(encoding="utf-8").replace(
            "-r requirements.in", "-r unexpected.in"
        ),
        encoding="utf-8",
    )

    result = _run_snapshot(tmp_path, tmp_path / "snapshot.json")

    assert result.returncode == 1
    assert "must include exactly '-r requirements.in' once" in result.stderr
