# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Validate and synchronize release metadata from the root .version file."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = "kogeler/remote-ssh-mcp-server"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
DEFAULT_ROOT = Path(__file__).resolve().parents[2]
VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
MODULE_VERSION_PATTERN = re.compile(
    r'^__version__ = "((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"$',
    re.MULTILINE,
)


class VersionError(ValueError):
    """Raised when release metadata violates the repository contract."""


@dataclass(frozen=True, order=True)
class Version:
    """Strict stable semantic version."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str, *, source: str) -> Version:
        """Parse a stable X.Y.Z version without prefixes or suffixes."""
        normalized = value.strip()
        match = VERSION_PATTERN.fullmatch(normalized)
        if match is None:
            raise VersionError(
                f"{source} must contain exactly one stable X.Y.Z version"
            )
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        """Return the canonical stable semantic version."""
        return f"{self.major}.{self.minor}.{self.patch}"


def _read_version(root: Path) -> Version:
    """Read the only human-maintained version source."""
    path = root / ".version"
    try:
        value = path.read_text(encoding="utf-8")
    except OSError as err:
        raise VersionError(f"cannot read .version: {err}") from err
    return Version.parse(value, source=".version")


def _read_pyproject_version(root: Path, *, source: str = "pyproject.toml") -> Version:
    """Read the package version mirror from pyproject.toml."""
    path = root / "pyproject.toml"
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        value = document["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as err:
        raise VersionError(f"cannot read {source} project.version: {err}") from err
    if not isinstance(value, str):
        raise VersionError(f"{source} project.version must be a string")
    return Version.parse(value, source=f"{source} project.version")


def _read_module_version(root: Path) -> Version:
    """Read the package version mirror from the module initializer."""
    path = root / "remote_ssh_mcp" / "__init__.py"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as err:
        raise VersionError(f"cannot read remote_ssh_mcp.__version__: {err}") from err
    matches = MODULE_VERSION_PATTERN.findall(content)
    if len(matches) != 1:
        raise VersionError(
            "remote_ssh_mcp/__init__.py must contain exactly one canonical "
            '__version__ = "X.Y.Z" assignment'
        )
    return Version.parse(matches[0], source="remote_ssh_mcp.__version__")


def _changelog_body(root: Path, version: Version) -> str:
    """Extract exactly one non-empty changelog section for a version."""
    path = root / "CHANGELOG.md"
    try:
        changelog = path.read_text(encoding="utf-8")
    except OSError as err:
        raise VersionError(f"cannot read CHANGELOG.md: {err}") from err

    heading = re.compile(
        rf"^## {re.escape(str(version))} - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$",
        re.MULTILINE,
    )
    matches = list(heading.finditer(changelog))
    if len(matches) != 1:
        raise VersionError(
            f"CHANGELOG.md must contain exactly one '## {version} - YYYY-MM-DD' section"
        )

    start = matches[0].end()
    next_heading = re.search(r"^## ", changelog[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading is not None else len(changelog)
    body = changelog[start:end].strip()
    if not body or not re.search(r"^- ", body, re.MULTILINE):
        raise VersionError(
            f"CHANGELOG.md section {version} must contain release entries"
        )
    return body


def _validate_current(root: Path) -> Version:
    """Validate every committed version mirror and release-note source."""
    version = _read_version(root)
    pyproject_version = _read_pyproject_version(root)
    module_version = _read_module_version(root)
    if pyproject_version != version:
        raise VersionError(
            f"pyproject.toml version {pyproject_version} does not match .version {version}; "
            "run the version sync command"
        )
    if module_version != version:
        raise VersionError(
            f"remote_ssh_mcp.__version__ {module_version} does not match .version "
            f"{version}; run the version sync command"
        )
    _changelog_body(root, version)
    return version


def _version_from_base_tree(root: Path) -> Version:
    """Read a base tree, falling back to metadata predating .version."""
    version_path = root / ".version"
    if version_path.is_file():
        return Version.parse(
            version_path.read_text(encoding="utf-8"), source="base .version"
        )
    return _read_pyproject_version(root, source="base pyproject.toml")


def _require_increment(
    current: Version,
    base: Version,
    unpublished_base: Version | None,
) -> None:
    """Require an increase, allowing recovery of an unpublished same version."""
    effective_base = unpublished_base if current == base and unpublished_base else base
    if current <= effective_base:
        raise VersionError(
            f".version {current} must be greater than base version {effective_base}"
        )


def _replace_exactly_once(
    content: str,
    pattern: re.Pattern[str],
    replacement: str,
    *,
    source: str,
) -> str:
    """Replace one version mirror without rewriting unrelated content."""
    updated, count = pattern.subn(replacement, content)
    if count != 1:
        raise VersionError(f"{source} must contain exactly one version mirror")
    return updated


def _sync_mirrors(root: Path, *, check: bool) -> Version:
    """Synchronize package metadata mirrors from .version."""
    version = _read_version(root)
    replacements = (
        (
            root / "pyproject.toml",
            re.compile(r'^(version\s*=\s*)"[^"\n]+"$', re.MULTILINE),
            rf'\g<1>"{version}"',
            "pyproject.toml",
        ),
        (
            root / "remote_ssh_mcp" / "__init__.py",
            re.compile(r'^__version__ = "[^"\n]+"$', re.MULTILINE),
            f'__version__ = "{version}"',
            "remote_ssh_mcp/__init__.py",
        ),
    )
    changed: list[tuple[Path, str]] = []
    for path, pattern, replacement, source in replacements:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as err:
            raise VersionError(f"cannot read {source}: {err}") from err
        updated = _replace_exactly_once(content, pattern, replacement, source=source)
        if updated != content:
            changed.append((path, updated))

    if check and changed:
        names = ", ".join(path.relative_to(root).as_posix() for path, _ in changed)
        raise VersionError(
            f"version mirrors do not match .version ({names}); run the version sync command"
        )
    if not check:
        for path, updated in changed:
            path.write_text(updated, encoding="utf-8")
    return version


def _release_notes(root: Path, version: Version) -> str:
    """Build a release body from the matching changelog section."""
    body = _changelog_body(root, version)
    changelog_url = f"{REPOSITORY_URL}/blob/{version}/CHANGELOG.md"
    return f"{body}\n\nFull changelog: {changelog_url}\n"


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="validate metadata and progression")
    base = check.add_mutually_exclusive_group()
    base.add_argument("--base-root", type=Path)
    base.add_argument("--base-version")
    check.add_argument("--unpublished-base-version")

    sync = commands.add_parser("sync", help="copy .version into package metadata")
    sync.add_argument("--check", action="store_true")

    notes = commands.add_parser("notes", help="write notes for the current version")
    notes.add_argument("--output", type=Path, required=True)

    commands.add_parser("current", help="print the current version")
    return parser


def _run(args: argparse.Namespace) -> None:
    """Execute a parsed command."""
    root = args.root.resolve()
    if args.command == "sync":
        print(_sync_mirrors(root, check=args.check))
        return
    if args.command == "current":
        print(_read_version(root))
        return

    version = _validate_current(root)
    if args.command == "check":
        base: Version | None = None
        if args.base_root is not None:
            base = _version_from_base_tree(args.base_root.resolve())
        elif args.base_version is not None:
            base = Version.parse(args.base_version, source="--base-version")
        if args.unpublished_base_version is not None and base is None:
            raise VersionError("--unpublished-base-version requires a base")
        if base is not None:
            unpublished_base = (
                Version.parse(
                    args.unpublished_base_version,
                    source="--unpublished-base-version",
                )
                if args.unpublished_base_version is not None
                else None
            )
            _require_increment(version, base, unpublished_base)
        print(version)
        return
    if args.command == "notes":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(_release_notes(root, version), encoding="utf-8")
        print(version)
        return
    raise AssertionError(f"unhandled command {args.command}")


def main() -> int:
    """Run the CLI and return a process exit status."""
    try:
        _run(_build_parser().parse_args())
    except VersionError as err:
        print(f"version error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
