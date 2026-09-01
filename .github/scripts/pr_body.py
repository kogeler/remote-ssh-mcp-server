# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Build a pull-request body with a changelog-managed section."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

START_MARKER = "<!-- remote-ssh-mcp-changelog:start -->"
END_MARKER = "<!-- remote-ssh-mcp-changelog:end -->"
MAX_CHANGELOG_BYTES = 1_000_000
MAX_PR_BODY_BYTES = 65_536
SECTION_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)


class PullRequestBodyError(ValueError):
    """Raised when changelog or PR body content is unsafe to synchronize."""


def _read_text(path: Path, *, label: str, maximum_bytes: int) -> str:
    if path.stat().st_size > maximum_bytes:
        raise PullRequestBodyError(f"{label} exceeds the supported size limit")
    return path.read_text(encoding="utf-8")


def latest_changelog_section(changelog: str) -> str:
    """Return the newest level-two section containing a bullet entry."""

    normalized = changelog.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(SECTION_HEADING.finditer(normalized))
    if not matches:
        raise PullRequestBodyError("CHANGELOG.md has no level-two section")
    for index, match in enumerate(matches):
        end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        )
        content = normalized[match.end() : end].strip()
        if content and re.search(r"^- ", content, re.MULTILINE) is not None:
            section = f"## {match.group(1).strip()}\n\n{content}"
            if START_MARKER in section or END_MARKER in section:
                raise PullRequestBodyError(
                    "CHANGELOG.md section contains a reserved marker"
                )
            return section
    raise PullRequestBodyError("CHANGELOG.md has no section containing a bullet entry")


def updated_body(existing_body: str, section: str) -> str:
    """Replace only the single managed block while preserving manual text."""

    normalized = existing_body.replace("\r\n", "\n").replace("\r", "\n")
    start_count = normalized.count(START_MARKER)
    end_count = normalized.count(END_MARKER)
    if start_count != end_count or start_count > 1:
        raise PullRequestBodyError("pull-request body has invalid managed markers")
    block = f"{START_MARKER}\n{section}\n{END_MARKER}"
    if start_count == 0:
        parts = [normalized.strip(), block]
    else:
        start = normalized.index(START_MARKER)
        end = normalized.index(END_MARKER)
        if end < start:
            raise PullRequestBodyError("pull-request body has invalid managed markers")
        end += len(END_MARKER)
        parts = [normalized[:start].strip(), block, normalized[end:].strip()]
    updated = "\n\n".join(part for part in parts if part) + "\n"
    if len(updated.encode("utf-8")) > MAX_PR_BODY_BYTES:
        raise PullRequestBodyError(
            "updated body exceeds the GitHub pull-request body limit"
        )
    return updated


def _run(arguments: argparse.Namespace) -> None:
    changelog = _read_text(
        arguments.changelog,
        label="CHANGELOG.md",
        maximum_bytes=MAX_CHANGELOG_BYTES,
    )
    existing = _read_text(
        arguments.existing_body,
        label="pull-request body",
        maximum_bytes=MAX_PR_BODY_BYTES,
    )
    arguments.output.write_text(
        updated_body(existing, latest_changelog_section(changelog)),
        encoding="utf-8",
    )


def main() -> int:
    """Render the managed body from command-line paths."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changelog", type=Path, required=True)
    parser.add_argument("--existing-body", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    try:
        _run(parser.parse_args())
    except (OSError, UnicodeError, PullRequestBodyError) as error:
        print(f"PR body error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
