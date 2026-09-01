# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Regression tests for changelog-managed pull-request bodies."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "pr_body.py"
START = "<!-- remote-ssh-mcp-changelog:start -->"
END = "<!-- remote-ssh-mcp-changelog:end -->"


def _run(tmp_path: Path, changelog: str, body: str = "") -> tuple[int, str, str]:
    changelog_path = tmp_path / "CHANGELOG.md"
    body_path = tmp_path / "body.md"
    output = tmp_path / "output.md"
    changelog_path.write_text(changelog, encoding="utf-8")
    body_path.write_text(body, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--changelog",
            str(changelog_path),
            "--existing-body",
            str(body_path),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return (
        result.returncode,
        output.read_text(encoding="utf-8") if output.exists() else "",
        result.stderr,
    )


def test_skips_empty_unreleased_and_preserves_manual_body(tmp_path: Path) -> None:
    code, output, error = _run(
        tmp_path,
        "# Changelog\n\n## Unreleased\n\n## 0.2.0 - 2026-08-29\n\n- Exact change.\n",
        "Manual context.\n",
    )

    assert code == 0, error
    assert output == (
        f"Manual context.\n\n{START}\n## 0.2.0 - 2026-08-29\n\n- Exact change.\n{END}\n"
    )


def test_replaces_only_the_managed_block(tmp_path: Path) -> None:
    existing = f"Before.\n\n{START}\n## Old\n\n- Old.\n{END}\n\nAfter.\n"
    code, output, error = _run(
        tmp_path,
        "# Changelog\r\n\r\n## 0.2.1 - 2026-09-01\r\n\r\n- Fresh.\r\n",
        existing,
    )

    assert code == 0, error
    assert output == (
        f"Before.\n\n{START}\n## 0.2.1 - 2026-09-01\n\n- Fresh.\n{END}\n\nAfter.\n"
    )


def test_rejects_missing_release_entries(tmp_path: Path) -> None:
    code, output, error = _run(tmp_path, "# Changelog\n\n## Unreleased\n")

    assert code == 1
    assert output == ""
    assert "bullet entry" in error


def test_rejects_malformed_or_reserved_markers(tmp_path: Path) -> None:
    code, output, error = _run(
        tmp_path,
        "# Changelog\n\n## 0.2.1 - 2026-09-01\n\n- Fresh.\n",
        f"Manual.\n\n{START}\n",
    )
    assert code == 1
    assert output == ""
    assert "invalid managed markers" in error

    code, output, error = _run(
        tmp_path,
        f"# Changelog\n\n## 0.2.1 - 2026-09-01\n\n- {START}\n",
    )
    assert code == 1
    assert output == ""
    assert "reserved marker" in error


def test_rejects_an_oversized_existing_body(tmp_path: Path) -> None:
    code, output, error = _run(
        tmp_path,
        "# Changelog\n\n## 0.2.1 - 2026-09-01\n\n- Fresh.\n",
        "x" * 65_537,
    )

    assert code == 1
    assert output == ""
    assert "size limit" in error
