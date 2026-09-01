# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Enforce the shared local and GitHub dependency-license policy."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml
from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / ".github" / "dependency-review-config.yml"
DEFAULT_ENVIRONMENTS = (
    ("runtime", ROOT / "requirements.txt", ROOT / "venv-runtime/bin/python"),
    ("development", ROOT / "requirements-dev.txt", ROOT / "venv-dev/bin/python"),
    ("lint", ROOT / "requirements-lint.txt", ROOT / "venv-lint/bin/python"),
    (
        "standalone",
        ROOT / "requirements-standalone.txt",
        ROOT / "venv-standalone/bin/python",
    ),
    ("documentation", ROOT / "requirements-docs.txt", ROOT / "venv-docs/bin/python"),
)
POLICY_KEYS = {
    "allow-dependencies-licenses",
    "allow-licenses",
    "comment-summary-in-pr",
    "fail-on-scopes",
    "fail-on-severity",
    "license-check",
    "vulnerability-check",
}
LOCK_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)\s+\\$")
PURL = re.compile(r"^pkg:pypi/([a-z0-9][a-z0-9.-]*)$")
SPDX_TOKEN = re.compile(r"\(|\)|AND|OR|WITH|[A-Za-z0-9][A-Za-z0-9.+-]*")
SPDX_OPERATORS = {"(", ")", "AND", "OR", "WITH"}
GPL_FAMILY_PREFIXES = ("AGPL-", "GPL-", "LGPL-")

LICENSE_ALIASES: dict[str, frozenset[str]] = {
    "Apache 2.0": frozenset({"Apache-2.0"}),
    "Apache Software License": frozenset({"Apache-2.0"}),
    "BSD License": frozenset({"BSD-2-Clause", "BSD-3-Clause"}),
    "MIT License": frozenset({"MIT"}),
    "Mozilla Public License 2.0 (MPL 2.0)": frozenset({"MPL-2.0"}),
    "PSFL": frozenset({"Python-2.0"}),
    "Python Software Foundation License": frozenset({"Python-2.0"}),
}
CLASSIFIER_LICENSES: dict[str, frozenset[str]] = {
    "License :: OSI Approved :: Apache Software License": frozenset({"Apache-2.0"}),
    "License :: OSI Approved :: BSD License": frozenset(
        {"BSD-2-Clause", "BSD-3-Clause"}
    ),
    "License :: OSI Approved :: GNU General Public License v2 (GPLv2)": (
        frozenset({"GPL-2.0-only"})
    ),
    "License :: OSI Approved :: ISC License (ISCL)": frozenset({"ISC"}),
    "License :: OSI Approved :: MIT License": frozenset({"MIT"}),
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": (
        frozenset({"MPL-2.0"})
    ),
    "License :: OSI Approved :: Python Software Foundation License": frozenset(
        {"Python-2.0"}
    ),
    "License :: OSI Approved :: The Unlicense (Unlicense)": frozenset({"Unlicense"}),
}
_METADATA_PROGRAM = r"""
import importlib.metadata as metadata
import json

packages = []
for distribution in metadata.distributions():
    record = distribution.metadata
    name = record.get("Name")
    if not name:
        continue
    packages.append({
        "name": name,
        "version": distribution.version,
        "license_expression": record.get("License-Expression"),
        "license": record.get("License"),
        "classifiers": [
            item for item in record.get_all("Classifier", [])
            if item.startswith("License ::")
        ],
    })
print(json.dumps({"packages": packages}, sort_keys=True))
"""


class LicensePolicyError(ValueError):
    """Dependency licenses or policy configuration are invalid."""


def _normalize_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _string_list(value: object, *, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise LicensePolicyError(f"{label} must be a non-empty string list")
    items = tuple(item.strip() for item in value)
    if len(items) != len(set(items)):
        raise LicensePolicyError(f"{label} contains a duplicate entry")
    return items


def _load_policy(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise LicensePolicyError(f"cannot read license policy: {error}") from error
    if not isinstance(document, dict) or set(document) != POLICY_KEYS:
        raise LicensePolicyError("license policy has an unexpected shape")
    if (
        document["fail-on-severity"] != "moderate"
        or document["fail-on-scopes"] != ["runtime", "development", "unknown"]
        or document["vulnerability-check"] is not True
        or document["license-check"] is not True
        or document["comment-summary-in-pr"] != "never"
    ):
        raise LicensePolicyError("dependency review fail-closed settings changed")

    allowed = frozenset(
        _string_list(document["allow-licenses"], label="allow-licenses")
    )
    for identifier in allowed:
        try:
            canonical = canonicalize_license_expression(identifier)
        except InvalidLicenseExpression as error:
            raise LicensePolicyError(
                f"allow-licenses contains an invalid SPDX identifier: {identifier}"
            ) from error
        if canonical != identifier or any(
            operator in canonical for operator in (" AND ", " OR ", " WITH ")
        ):
            raise LicensePolicyError(
                f"allow-licenses must contain canonical individual identifiers: {identifier}"
            )
    gpl_family = sorted(
        identifier
        for identifier in allowed
        if identifier.startswith(GPL_FAMILY_PREFIXES)
    )
    if gpl_family:
        raise LicensePolicyError(
            "GPL-family licenses cannot be globally allowed: " + ", ".join(gpl_family)
        )

    exceptions: set[str] = set()
    for value in _string_list(
        document["allow-dependencies-licenses"],
        label="allow-dependencies-licenses",
    ):
        match = PURL.fullmatch(value)
        if match is None:
            raise LicensePolicyError(
                f"license exception is not a package-only PyPI purl: {value}"
            )
        item = _normalize_package(match.group(1))
        if item in exceptions:
            raise LicensePolicyError(f"duplicate normalized license exception: {value}")
        exceptions.add(item)
    return allowed, frozenset(exceptions)


def _lock_packages(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise LicensePolicyError(f"cannot read {path.name}: {error}") from error
    packages: dict[str, str] = {}
    for line in lines:
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        match = LOCK_PIN.fullmatch(line)
        if match is None:
            raise LicensePolicyError(f"{path.name}: invalid lock entry: {line}")
        name = _normalize_package(match.group(1))
        if name in packages:
            raise LicensePolicyError(f"{path.name}: duplicate package {name}")
        packages[name] = match.group(2)
    if not packages:
        raise LicensePolicyError(f"{path.name}: lock is empty")
    return packages


def _parse_inventory(document: object) -> list[dict[str, object]]:
    if not isinstance(document, dict) or set(document) != {"packages"}:
        raise LicensePolicyError("license inventory must contain only a packages list")
    packages = document["packages"]
    if not isinstance(packages, list):
        raise LicensePolicyError("license inventory packages must be a list")
    result: list[dict[str, object]] = []
    for package in packages:
        if not isinstance(package, dict) or set(package) != {
            "classifiers",
            "license",
            "license_expression",
            "name",
            "version",
        }:
            raise LicensePolicyError("license inventory package has an invalid shape")
        if (
            not isinstance(package["name"], str)
            or not package["name"]
            or not isinstance(package["version"], str)
            or not package["version"]
            or package["license_expression"] is not None
            and not isinstance(package["license_expression"], str)
            or package["license"] is not None
            and not isinstance(package["license"], str)
            or not isinstance(package["classifiers"], list)
            or not all(isinstance(item, str) for item in package["classifiers"])
        ):
            raise LicensePolicyError("license inventory package has invalid values")
        result.append(package)
    return result


def _load_inventory(path: Path) -> list[dict[str, object]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LicensePolicyError(f"cannot read license inventory: {error}") from error
    return _parse_inventory(document)


def _environment_inventory(
    label: str, lock: Path, python: Path
) -> list[dict[str, object]]:
    expected = _lock_packages(lock)
    result = subprocess.run(
        [str(python), "-I", "-c", _METADATA_PROGRAM],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "no diagnostic output"
        raise LicensePolicyError(f"cannot inspect {label} environment: {detail}")
    try:
        installed = _parse_inventory(json.loads(result.stdout))
    except json.JSONDecodeError as error:
        raise LicensePolicyError(
            f"{label} environment returned invalid metadata JSON: {error}"
        ) from error
    by_name: dict[str, dict[str, object]] = {}
    for package in installed:
        name = _normalize_package(str(package["name"]))
        if name in expected:
            if name in by_name:
                raise LicensePolicyError(f"{label}: duplicate installed package {name}")
            by_name[name] = package
    missing = sorted(set(expected) - set(by_name))
    mismatched = sorted(
        f"{name}: installed {by_name[name]['version']}, locked {version}"
        for name, version in expected.items()
        if name in by_name and by_name[name]["version"] != version
    )
    if missing or mismatched:
        details = [*(f"missing {name}" for name in missing), *mismatched]
        raise LicensePolicyError(
            f"{label} environment differs from its lock: " + "; ".join(details)
        )
    return [by_name[name] for name in sorted(by_name)]


def _spdx_identifiers(expression: str) -> frozenset[str] | None:
    matches = list(SPDX_TOKEN.finditer(expression))
    if not matches:
        return None
    position = 0
    tokens: list[str] = []
    for match in matches:
        if expression[position : match.start()].strip():
            return None
        tokens.append(match.group(0))
        position = match.end()
    if expression[position:].strip():
        return None
    identifiers = frozenset(token for token in tokens if token not in SPDX_OPERATORS)
    return identifiers or None


def _declared_licenses(package: dict[str, object]) -> frozenset[str]:
    expression = package["license_expression"]
    if isinstance(expression, str) and expression.strip():
        try:
            canonical = canonicalize_license_expression(expression.strip())
        except InvalidLicenseExpression as error:
            raise LicensePolicyError(
                f"{package['name']}=={package['version']}: invalid License-Expression"
            ) from error
        identifiers = _spdx_identifiers(canonical)
        assert identifiers is not None
        return identifiers

    license_value = package["license"]
    if isinstance(license_value, str) and license_value.strip():
        value = license_value.strip()
        if value in LICENSE_ALIASES:
            return LICENSE_ALIASES[value]
        try:
            canonical = canonicalize_license_expression(value)
        except InvalidLicenseExpression:
            canonical = ""
        identifiers = _spdx_identifiers(canonical)
        if identifiers is not None:
            return identifiers

    declared: set[str] = set()
    classifiers = package["classifiers"]
    assert isinstance(classifiers, list)
    unknown: list[str] = []
    for classifier in classifiers:
        if classifier in CLASSIFIER_LICENSES:
            declared.update(CLASSIFIER_LICENSES[classifier])
        elif classifier.startswith("License ::"):
            unknown.append(classifier)
    if unknown:
        raise LicensePolicyError(
            f"{package['name']}=={package['version']}: unknown license classifiers: "
            + ", ".join(sorted(unknown))
        )
    if not declared:
        raise LicensePolicyError(
            f"{package['name']}=={package['version']}: no recognized license metadata"
        )
    return frozenset(declared)


def _audit_packages(
    packages: list[dict[str, object]],
    allowed: frozenset[str],
    exceptions: frozenset[str],
) -> tuple[int, frozenset[str]]:
    unique: set[tuple[str, str]] = set()
    used_exceptions: set[str] = set()
    failures: list[str] = []
    for package in packages:
        name = _normalize_package(str(package["name"]))
        version = str(package["version"])
        identity = (name, version)
        unique.add(identity)
        declared = _declared_licenses(package)
        rejected = sorted(declared - allowed)
        rejected_non_gpl = [
            identifier
            for identifier in rejected
            if not identifier.startswith(GPL_FAMILY_PREFIXES)
        ]
        if name in exceptions and not rejected_non_gpl:
            used_exceptions.add(name)
            continue
        if rejected:
            failures.append(f"{name}=={version}: {', '.join(rejected)}")
    if failures:
        raise LicensePolicyError(
            "unapproved dependency licenses:\n- " + "\n- ".join(sorted(failures))
        )
    return len(unique), frozenset(used_exceptions)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument(
        "--inventory",
        type=Path,
        help="audit a deterministic JSON inventory instead of local lock environments",
    )
    return parser


def _run(arguments: argparse.Namespace) -> None:
    allowed, exceptions = _load_policy(arguments.policy)
    if arguments.inventory is not None:
        packages = _load_inventory(arguments.inventory)
    else:
        packages = []
        for label, lock, python in DEFAULT_ENVIRONMENTS:
            packages.extend(_environment_inventory(label, lock, python))
    count, used_exceptions = _audit_packages(packages, allowed, exceptions)
    stale = sorted(exceptions - used_exceptions)
    if stale:
        details = ", ".join(stale)
        raise LicensePolicyError(f"stale dependency license exceptions: {details}")
    print(
        f"License policy accepted {count} unique package versions and "
        f"{len(used_exceptions)} reviewed package exceptions"
    )


def main() -> int:
    """Validate policy and dependency metadata."""

    try:
        _run(_build_parser().parse_args())
    except LicensePolicyError as error:
        print(f"license policy error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
