# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Behavior tests for the local dependency-license policy gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "license_policy.py"


def _package(
    name: str,
    version: str,
    *,
    expression: str | None = None,
    license_value: str | None = None,
    classifiers: list[str] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "license_expression": expression,
        "license": license_value,
        "classifiers": classifiers or [],
    }


def _run(
    tmp_path: Path,
    packages: list[dict[str, object]],
    *,
    allowed: list[str] | None = None,
    exceptions: list[str] | None = None,
    license_check: bool = True,
) -> subprocess.CompletedProcess[str]:
    policy = tmp_path / "policy.yml"
    inventory = tmp_path / "inventory.json"
    policy.write_text(
        yaml.safe_dump(
            {
                "fail-on-severity": "moderate",
                "fail-on-scopes": ["runtime", "development", "unknown"],
                "vulnerability-check": True,
                "license-check": license_check,
                "comment-summary-in-pr": "never",
                "allow-licenses": allowed
                or ["Apache-2.0", "BSD-3-Clause", "MIT", "MIT-0"],
                "allow-dependencies-licenses": exceptions or ["pkg:pypi/bundler"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    inventory.write_text(json.dumps({"packages": packages}), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--policy",
            str(policy),
            "--inventory",
            str(inventory),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_accepts_approved_expressions_and_one_package_exception(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        [
            _package("example", "2.0", expression="MIT AND MIT-0"),
            _package("bundler", "1.0", expression="GPL-2.0-or-later"),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "License policy accepted 2 unique package versions and "
        "1 reviewed package exceptions\n"
    )


def test_rejects_unapproved_license(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [
            _package("example", "2.0", expression="GPL-3.0-only"),
            _package("bundler", "1.0", expression="GPL-2.0-or-later"),
        ],
    )

    assert result.returncode == 1
    assert "unapproved dependency licenses" in result.stderr
    assert "example==2.0: GPL-3.0-only" in result.stderr


def test_package_exception_cannot_hide_a_non_gpl_license(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [_package("bundler", "1.0", expression="GPL-2.0-only AND CC-BY-4.0")],
    )

    assert result.returncode == 1
    assert "unapproved dependency licenses" in result.stderr
    assert "bundler==1.0: CC-BY-4.0, GPL-2.0-only" in result.stderr


def test_exception_uses_inventory_version_and_rejects_missing_package(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        [_package("bundler", "2.0", expression="GPL-2.0-or-later")],
    )

    assert result.returncode == 0, result.stderr
    assert "1 reviewed package exceptions" in result.stdout

    result = _run(tmp_path, [_package("example", "1.0", expression="MIT")])
    assert result.returncode == 1
    assert "stale dependency license exceptions: bundler" in result.stderr


def test_rejects_global_gpl_family_license_and_versioned_exception(
    tmp_path: Path,
) -> None:
    packages = [_package("bundler", "1.0", expression="GPL-2.0-or-later")]
    result = _run(
        tmp_path,
        packages,
        allowed=["MIT", "GPL-2.0-or-later"],
    )
    assert result.returncode == 1
    assert "GPL-family licenses cannot be globally allowed" in result.stderr

    result = _run(
        tmp_path,
        packages,
        exceptions=["pkg:pypi/bundler@1.0"],
    )
    assert result.returncode == 1
    assert "not a package-only PyPI purl" in result.stderr

    result = _run(tmp_path, packages, allowed=["not-a-real-license"])
    assert result.returncode == 1
    assert "invalid SPDX identifier" in result.stderr


def test_rejects_disabled_license_check(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [_package("bundler", "1.0", expression="GPL-2.0-or-later")],
        license_check=False,
    )

    assert result.returncode == 1
    assert "fail-closed settings changed" in result.stderr


def test_falls_back_to_recognized_trove_classifiers(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [
            _package(
                "example",
                "1.0",
                license_value="UNKNOWN",
                classifiers=["License :: OSI Approved :: MIT License"],
            ),
            _package("bundler", "1.0", expression="GPL-2.0-or-later"),
        ],
    )

    assert result.returncode == 0, result.stderr


def test_repository_policy_accepts_reviewed_github_aggregate_findings(
    tmp_path: Path,
) -> None:
    """Reproduce the dependency-graph expressions that motivated the policy."""

    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "packages": [
                    _package("altgraph", "0.17.5", expression="MIT AND MIT-0"),
                    _package("cffi", "2.1.1", expression="MIT-0"),
                    _package("mcp", "2.0.0", expression="MIT AND Python-2.0"),
                    _package(
                        "pyinstaller",
                        "6.22.2",
                        expression="GPL-2.0-only AND GPL-2.0-or-later",
                    ),
                    _package(
                        "pyinstaller-hooks-contrib",
                        "2026.7",
                        expression=(
                            "Apache-2.0 AND GPL-1.0-or-later AND "
                            "GPL-2.0-only AND GPL-2.0-or-later"
                        ),
                    ),
                    _package(
                        "typing-extensions",
                        "4.16.0",
                        expression=(
                            "Python-2.0 AND GPL-1.0-or-later AND BSD-3-Clause AND 0BSD"
                        ),
                    ),
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--policy",
            str(ROOT / ".github/dependency-review-config.yml"),
            "--inventory",
            str(inventory),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (
        "6 unique package versions and 3 reviewed package exceptions" in result.stdout
    )
