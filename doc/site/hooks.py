# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""MkDocs hooks for repository links and deterministic project-site files."""

from __future__ import annotations

import gzip
import os
import re
import shutil
from pathlib import Path
from typing import Any

REPOSITORY_BLOB = "https://github.com/kogeler/remote-ssh-mcp-server/blob/main"
NORMALIZATION_EPOCH = 315_532_800
NORMALIZATION_DATE = "1980-01-01"
_LINK_PATTERN = re.compile(r"\]\((?P<target>(?:\.\./)+[^)#]+)(?P<fragment>#[^)]*)?\)")


def on_page_markdown(
    markdown: str, *, page: Any, config: Any, **_kwargs: object
) -> str:
    """Rewrite links outside docs_dir to corresponding repository files."""

    source_dir = Path(page.file.abs_src_path).parent
    docs_dir = Path(config["docs_dir"]).resolve()
    repository = docs_dir.parent

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        resolved = (source_dir / target).resolve()
        try:
            resolved.relative_to(docs_dir)
        except ValueError:
            try:
                repository_path = resolved.relative_to(repository).as_posix()
            except ValueError:
                return match.group(0)
            fragment = match.group("fragment") or ""
            return f"]({REPOSITORY_BLOB}/{repository_path}{fragment})"
        return match.group(0)

    return _LINK_PATTERN.sub(replace, markdown)


def on_post_build(*, config: Any, **_kwargs: object) -> None:
    """Publish root files and normalize all generated site evidence."""

    docs_dir = Path(config["docs_dir"])
    site_dir = Path(config["site_dir"])
    for name in ("llms.txt", "robots.txt"):
        shutil.copyfile(docs_dir / "site" / name, site_dir / name)

    sitemap = site_dir / "sitemap.xml"
    content = re.sub(
        r"<lastmod>[0-9]{4}-[0-9]{2}-[0-9]{2}</lastmod>",
        f"<lastmod>{NORMALIZATION_DATE}</lastmod>",
        sitemap.read_text(encoding="utf-8"),
    ).encode()
    sitemap.write_bytes(content)
    (site_dir / "sitemap.xml.gz").write_bytes(gzip.compress(content, mtime=0))

    entries = sorted(site_dir.rglob("*"), reverse=True)
    for path in entries:
        if path.is_symlink():
            raise ValueError(f"generated site entry is a symlink: {path}")
        path.chmod(0o755 if path.is_dir() else 0o644)
        os.utime(path, (NORMALIZATION_EPOCH, NORMALIZATION_EPOCH))
    os.utime(site_dir, (NORMALIZATION_EPOCH, NORMALIZATION_EPOCH))
