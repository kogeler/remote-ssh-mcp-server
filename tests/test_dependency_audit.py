# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Regression tests for the fork-compatible dependency audit policy."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
AUDIT_SCRIPT = ROOT / ".github" / "scripts" / "dependency_audit.py"


def _run_audit(
    tmp_path: Path,
    *,
    vulnerabilities: list[str],
    exceptions: list[str],
    report_version: str = "1.0.0",
    exception_version: str = "1.0.0",
) -> subprocess.CompletedProcess[str]:
    """Run the helper against deterministic report and config documents."""
    report = tmp_path / "report.json"
    config = tmp_path / "exceptions.json"
    report.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "Example_Package",
                        "version": report_version,
                        "vulns": [{"id": item} for item in vulnerabilities],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config.write_text(
        json.dumps(
            {
                "exceptions": [
                    {
                        "package": "example-package",
                        "version": exception_version,
                        "vulnerabilities": exceptions,
                        "reason": "Test exception",
                    }
                ]
                if exceptions
                else []
            }
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            sys.executable,
            str(AUDIT_SCRIPT),
            "--report",
            str(report),
            "--exceptions",
            str(config),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_accepts_only_exact_reviewed_findings(tmp_path: Path) -> None:
    """An exact package, version, and advisory set passes."""
    result = _run_audit(
        tmp_path,
        vulnerabilities=["PYSEC-2", "PYSEC-1", "PYSEC-1"],
        exceptions=["PYSEC-1", "PYSEC-2"],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Dependency audit accepted 2 exact reviewed findings\n"


def test_accepts_an_empty_clean_graph(tmp_path: Path) -> None:
    """A clean report needs no placeholder exception."""
    result = _run_audit(tmp_path, vulnerabilities=[], exceptions=[])

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Dependency audit accepted 0 exact reviewed findings\n"


def test_live_audit_includes_the_complete_published_graph() -> None:
    """The strict audit uses the complete hash-locked dependency graph."""
    content = AUDIT_SCRIPT.read_text(encoding="utf-8")

    assert "LOCAL_PROJECTS" not in content
    assert "DEFAULT_AUDIT_LOCKS" in content
    assert '"requirements-dev.txt"' in content
    assert '"requirements-docs.txt"' in content
    assert '"requirements-standalone.txt"' in content
    assert '"--strict",\n            "--no-deps",' in content
    assert '"--disable-pip",' in content


def test_rejects_unexpected_vulnerability(tmp_path: Path) -> None:
    """A newly reported advisory remains blocking."""
    result = _run_audit(
        tmp_path,
        vulnerabilities=["PYSEC-1", "PYSEC-2"],
        exceptions=["PYSEC-1"],
    )

    assert result.returncode == 1
    assert "unexpected vulnerabilities" in result.stderr
    assert "PYSEC-2" in result.stderr


def test_rejects_stale_exception(tmp_path: Path) -> None:
    """A fixed or removed advisory requires exception cleanup."""
    result = _run_audit(
        tmp_path,
        vulnerabilities=["PYSEC-1"],
        exceptions=["PYSEC-1", "PYSEC-2"],
    )

    assert result.returncode == 1
    assert "stale vulnerability exceptions" in result.stderr
    assert "PYSEC-2" in result.stderr


def test_rejects_exception_for_another_package_version(tmp_path: Path) -> None:
    """An exception cannot float across dependency upgrades."""
    result = _run_audit(
        tmp_path,
        vulnerabilities=["PYSEC-1"],
        exceptions=["PYSEC-1"],
        report_version="2.0.0",
        exception_version="1.0.0",
    )

    assert result.returncode == 1
    assert "unexpected vulnerabilities" in result.stderr
    assert "stale vulnerability exceptions" not in result.stderr


def test_rejects_duplicate_exception(tmp_path: Path) -> None:
    """One advisory cannot be approved twice in the same config."""
    report = tmp_path / "report.json"
    config = tmp_path / "exceptions.json"
    report.write_text('{"dependencies": []}\n', encoding="utf-8")
    config.write_text(
        json.dumps(
            {
                "exceptions": [
                    {
                        "package": "example",
                        "version": "1",
                        "vulnerabilities": ["PYSEC-1", "PYSEC-1"],
                        "reason": "Test exception",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(AUDIT_SCRIPT),
            "--report",
            str(report),
            "--exceptions",
            str(config),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "duplicate exception" in result.stderr


def test_rejects_malformed_report_and_config(tmp_path: Path) -> None:
    """Unknown JSON shapes cannot weaken comparison semantics."""
    report = tmp_path / "report.json"
    config = tmp_path / "exceptions.json"
    report.write_text('{"packages": []}\n', encoding="utf-8")
    config.write_text('{"exceptions": [], "unknown": true}\n', encoding="utf-8")

    first = subprocess.run(
        [
            sys.executable,
            str(AUDIT_SCRIPT),
            "--report",
            str(report),
            "--exceptions",
            str(config),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 1
    assert "dependencies list" in first.stderr

    report.write_text('{"dependencies": []}\n', encoding="utf-8")
    second = subprocess.run(
        [
            sys.executable,
            str(AUDIT_SCRIPT),
            "--report",
            str(report),
            "--exceptions",
            str(config),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 1
    assert "only an exceptions list" in second.stderr
