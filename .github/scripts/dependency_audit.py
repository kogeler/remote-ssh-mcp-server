# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Audit the frozen environment against exact vulnerability exceptions."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXCEPTIONS = DEFAULT_ROOT / ".github" / "dependency-audit-exceptions.json"
DEFAULT_AUDIT_LOCKS = (
    DEFAULT_ROOT / "requirements-dev.txt",
    DEFAULT_ROOT / "requirements-docs.txt",
    DEFAULT_ROOT / "requirements-standalone.txt",
)
Finding = tuple[str, str, str]


class AuditError(ValueError):
    """Raised when audit data or its exception contract is invalid."""


def _normalize_package(name: str) -> str:
    """Normalize a package name according to Python comparison rules."""
    return re.sub(r"[-_.]+", "-", name).casefold()


def _load_json(path: Path, *, source: str) -> object:
    """Load JSON with a concise source-specific error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise AuditError(f"cannot read {source}: {err}") from err


def _audit_findings(report: object) -> frozenset[Finding]:
    """Extract unique package, version, and advisory findings."""
    if not isinstance(report, dict) or not isinstance(
        dependencies := report.get("dependencies"), list
    ):
        raise AuditError("pip-audit report must contain a dependencies list")

    findings: set[Finding] = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise AuditError("pip-audit dependency entry must be an object")
        name = dependency.get("name")
        version = dependency.get("version")
        vulnerabilities = dependency.get("vulns")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(vulnerabilities, list)
        ):
            raise AuditError("pip-audit dependency entry has an invalid shape")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict) or not isinstance(
                vulnerability_id := vulnerability.get("id"), str
            ):
                raise AuditError("pip-audit vulnerability entry has an invalid shape")
            findings.add((_normalize_package(name), version, vulnerability_id))
    return frozenset(findings)


def _format_finding(finding: Finding) -> str:
    """Format a finding for deterministic diagnostics."""
    package, version, vulnerability_id = finding
    return f"{package}=={version}: {vulnerability_id}"


def _allowed_findings(config: object) -> frozenset[Finding]:
    """Expand the exact reviewed exception configuration."""
    if (
        not isinstance(config, dict)
        or set(config) != {"exceptions"}
        or not isinstance(exceptions := config.get("exceptions"), list)
    ):
        raise AuditError("exception config must contain only an exceptions list")

    allowed: set[Finding] = set()
    for exception in exceptions:
        if not isinstance(exception, dict) or set(exception) != {
            "package",
            "version",
            "vulnerabilities",
            "reason",
        }:
            raise AuditError("exception entry has an invalid shape")
        package = exception.get("package")
        version = exception.get("version")
        vulnerabilities = exception.get("vulnerabilities")
        reason = exception.get("reason")
        if (
            not isinstance(package, str)
            or not package
            or not isinstance(version, str)
            or not version
            or not isinstance(vulnerabilities, list)
            or not vulnerabilities
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise AuditError("exception entry has an invalid shape")
        for vulnerability_id in vulnerabilities:
            if not isinstance(vulnerability_id, str) or not vulnerability_id:
                raise AuditError(
                    "exception vulnerability ID must be a non-empty string"
                )
            finding = (_normalize_package(package), version, vulnerability_id)
            if finding in allowed:
                raise AuditError(f"duplicate exception for {_format_finding(finding)}")
            allowed.add(finding)
    return frozenset(allowed)


def _run_pip_audit(lock: Path) -> object:
    """Strictly audit one complete hash-locked dependency graph."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "--strict",
            "--no-deps",
            "--disable-pip",
            "--requirement",
            str(lock),
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or "no diagnostic output"
        raise AuditError(f"pip-audit failed: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as err:
        raise AuditError(f"pip-audit returned invalid JSON: {err}") from err


def _run_live_audits() -> object:
    """Combine development and standalone reports without dropping duplicates."""
    dependencies: list[object] = []
    for lock in DEFAULT_AUDIT_LOCKS:
        report = _run_pip_audit(lock)
        if not isinstance(report, dict) or not isinstance(
            graph := report.get("dependencies"), list
        ):
            raise AuditError(f"pip-audit returned an invalid report for {lock.name}")
        dependencies.extend(graph)
    return {"dependencies": dependencies}


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    parser.add_argument(
        "--report",
        type=Path,
        help="read existing pip-audit JSON for deterministic tests",
    )
    return parser


def _run(args: argparse.Namespace) -> None:
    """Compare actual findings with the exact reviewed exception set."""
    report = (
        _load_json(args.report, source="pip-audit report")
        if args.report is not None
        else _run_live_audits()
    )
    actual = _audit_findings(report)
    allowed = _allowed_findings(_load_json(args.exceptions, source="exception config"))
    unexpected = sorted(actual - allowed)
    stale = sorted(allowed - actual)
    if unexpected:
        details = "\n".join(f"- {_format_finding(item)}" for item in unexpected)
        raise AuditError(f"unexpected vulnerabilities:\n{details}")
    if stale:
        details = "\n".join(f"- {_format_finding(item)}" for item in stale)
        raise AuditError(f"stale vulnerability exceptions:\n{details}")
    print(f"Dependency audit accepted {len(actual)} exact reviewed findings")


def main() -> int:
    """Run the audit helper and return its process status."""
    try:
        _run(_build_parser().parse_args())
    except AuditError as err:
        print(f"dependency audit error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
