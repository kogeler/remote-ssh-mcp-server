# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Regression tests for the release-version contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
VERSION_FILE = ROOT / ".version"
VERSION_SCRIPT = ROOT / ".github" / "scripts" / "version.py"


def _write_repository(
    root: Path,
    *,
    version: str = "1.0.0",
    pyproject_version: str = "1.0.0",
    module_version: str = "1.0.0",
    changelog: str | None = None,
    include_version: bool = True,
) -> None:
    """Create the version-bearing portion of a test repository."""
    package = root / "remote_ssh_mcp"
    package.mkdir(parents=True)
    if include_version:
        (root / ".version").write_text(f"{version}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "remote-ssh-mcp"\nversion = "{pyproject_version}"\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        f'"""Package."""\n\n__version__ = "{module_version}"\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        changelog
        or "# Changelog\n\n## 1.0.0 - 2026-08-19\n\n### Added\n\n- Release entry.\n",
        encoding="utf-8",
    )


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the version helper through its production CLI."""
    return subprocess.run(
        [sys.executable, str(VERSION_SCRIPT), "--root", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("version", ["v1.0.0", "1.0", "1.0.0b1", "01.0.0"])
def test_rejects_noncanonical_versions(tmp_path: Path, version: str) -> None:
    """Only a stable canonical semantic version is eligible for publication."""
    _write_repository(tmp_path, version=version)

    result = _run(tmp_path, "check")

    assert result.returncode == 1
    assert "stable X.Y.Z" in result.stderr


@pytest.mark.parametrize(
    ("pyproject_version", "module_version", "message"),
    [
        ("0.6.2", "1.0.0", "pyproject.toml version"),
        ("1.0.0", "0.6.2", "remote_ssh_mcp.__version__"),
    ],
)
def test_requires_both_mirrors_to_match(
    tmp_path: Path,
    pyproject_version: str,
    module_version: str,
    message: str,
) -> None:
    """Package metadata is checked generated state, not another version source."""
    _write_repository(
        tmp_path,
        pyproject_version=pyproject_version,
        module_version=module_version,
    )

    result = _run(tmp_path, "check")

    assert result.returncode == 1
    assert message in result.stderr


@pytest.mark.parametrize(
    "changelog",
    [
        "# Changelog\n\n## Unreleased\n\n- Pending.\n",
        "# Changelog\n\n## 1.0.0\n\n- Missing date.\n",
        "# Changelog\n\n## 1.0.0 - 2026-08-19\n\nNo entries.\n",
    ],
)
def test_requires_matching_nonempty_changelog_section(
    tmp_path: Path, changelog: str
) -> None:
    """A release cannot be published without curated notes for its version."""
    _write_repository(tmp_path, changelog=changelog)

    result = _run(tmp_path, "check")

    assert result.returncode == 1
    assert "CHANGELOG.md" in result.stderr


@pytest.mark.parametrize("base", ["1.0.0", "1.0.1", "2.0.0"])
def test_requires_strict_version_increment(tmp_path: Path, base: str) -> None:
    """Unchanged and decreasing versions fail before merge or release."""
    _write_repository(tmp_path)

    result = _run(tmp_path, "check", "--base-version", base)

    assert result.returncode == 1
    assert f"must be greater than base version {base}" in result.stderr


def test_accepts_strict_version_increment(tmp_path: Path) -> None:
    """A greater version with synchronized metadata is accepted."""
    _write_repository(tmp_path)

    result = _run(tmp_path, "check", "--base-version", "0.6.2")

    assert result.returncode == 0
    assert result.stdout == "1.0.0\n"


def test_allows_same_version_only_against_unpublished_baseline(tmp_path: Path) -> None:
    """A failed publication can be recovered without inventing a new version."""
    _write_repository(tmp_path)

    accepted = _run(
        tmp_path,
        "check",
        "--base-version",
        "1.0.0",
        "--unpublished-base-version",
        "0.6.2",
    )
    rejected = _run(tmp_path, "check", "--unpublished-base-version", "0.6.2")

    assert accepted.returncode == 0
    assert rejected.returncode == 1
    assert "requires a base" in rejected.stderr


def test_base_tree_falls_back_to_legacy_pyproject(tmp_path: Path) -> None:
    """Introducing .version compares against the previous package metadata."""
    source = tmp_path / "source"
    base = tmp_path / "base"
    source.mkdir()
    base.mkdir()
    _write_repository(source)
    _write_repository(base, pyproject_version="0.6.2", include_version=False)

    result = _run(source, "check", "--base-root", str(base))

    assert result.returncode == 0


def test_sync_updates_only_both_version_mirrors(tmp_path: Path) -> None:
    """The explicit sync command preserves unrelated metadata."""
    _write_repository(tmp_path, pyproject_version="0.6.2", module_version="0.6.2")
    pyproject = tmp_path / "pyproject.toml"
    initializer = tmp_path / "remote_ssh_mcp" / "__init__.py"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8") + 'description = "Preserved"\n',
        encoding="utf-8",
    )

    result = _run(tmp_path, "sync")

    assert result.returncode == 0
    assert 'version = "1.0.0"' in pyproject.read_text(encoding="utf-8")
    assert 'description = "Preserved"' in pyproject.read_text(encoding="utf-8")
    assert '__version__ = "1.0.0"' in initializer.read_text(encoding="utf-8")
    assert _run(tmp_path, "sync", "--check").returncode == 0


def test_release_notes_use_only_matching_section(tmp_path: Path) -> None:
    """Release notes contain current entries and a tag-stable changelog link."""
    _write_repository(
        tmp_path,
        changelog=(
            "# Changelog\n\n"
            "## Unreleased\n\n- Future entry.\n\n"
            "## 1.0.0 - 2026-08-19\n\n### Added\n\n- Current entry.\n\n"
            "## 0.6.2 - 2026-08-01\n\n- Old entry.\n"
        ),
    )
    output = tmp_path / "nested" / "release-notes.md"

    result = _run(tmp_path, "notes", "--output", str(output))

    assert result.returncode == 0
    assert output.read_text(encoding="utf-8") == (
        "### Added\n\n"
        "- Current entry.\n\n"
        "Full changelog: "
        "https://github.com/kogeler/remote-ssh-mcp-server/blob/1.0.0/CHANGELOG.md\n"
    )


def test_repository_metadata_is_release_ready() -> None:
    """Committed metadata satisfies the same executable contract."""
    result = _run(ROOT, "check")

    assert result.returncode == 0, result.stderr
    assert result.stdout == VERSION_FILE.read_text(encoding="utf-8")
