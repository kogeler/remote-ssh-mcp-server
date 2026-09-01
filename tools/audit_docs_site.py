# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Audit the rendered Remote SSH MCP documentation site without network access."""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

NORMALIZATION_EPOCH = 315_532_800
NORMALIZATION_DATE = "1980-01-01"
_SITEMAP = re.compile(
    rb'\A<\?xml version="1\.0" encoding="UTF-8"\?>\s*'
    rb'<urlset xmlns="http://www\.sitemaps\.org/schemas/sitemap/0\.9">'
    rb"(?P<body>.*)</urlset>\s*\Z",
    re.DOTALL,
)
_SITEMAP_ENTRY = re.compile(
    rb"\s*<url>\s*<loc>(?P<location>https://[A-Za-z0-9./_-]+)</loc>\s*"
    rb"<lastmod>(?P<lastmod>[0-9]{4}-[0-9]{2}-[0-9]{2})</lastmod>\s*</url>",
)


class AuditError(RuntimeError):
    """Rendered documentation violates the publication contract."""


@dataclass
class HtmlDocument:
    """HTML facts required for local route and metadata validation."""

    ids: set[str] = field(default_factory=set)
    references: list[tuple[str, str]] = field(default_factory=list)
    canonicals: list[str] = field(default_factory=list)
    h1_count: int = 0
    title_text: list[str] = field(default_factory=list)
    in_title: bool = False


class SiteHtmlParser(HTMLParser):
    """Collect anchors, references, headings, and canonical metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document = HtmlDocument()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if identifier := values.get("id"):
            self.document.ids.add(identifier)
        if tag == "h1":
            self.document.h1_count += 1
        if tag == "title":
            self.document.in_title = True
        if tag == "img" and "alt" not in values:
            raise AuditError("generated image is missing an alt attribute")
        for attribute in ("href", "src"):
            if target := values.get(attribute):
                self.document.references.append((tag, target))
        if (
            tag == "link"
            and "canonical" in (values.get("rel") or "").split()
            and (canonical := values.get("href"))
        ):
            self.document.canonicals.append(canonical)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.document.in_title = False

    def handle_data(self, data: str) -> None:
        if self.document.in_title:
            self.document.title_text.append(data)


def _route_for_page(path: Path, site_dir: Path) -> str:
    relative = path.relative_to(site_dir)
    if relative.name == "index.html":
        parent = relative.parent.as_posix()
        return f"{parent}/" if parent != "." else ""
    return relative.as_posix()


def _parse_document(path: Path) -> HtmlDocument:
    parser = SiteHtmlParser()
    try:
        parser.feed(path.read_text(encoding="utf-8"))
    except AuditError as error:
        raise AuditError(f"{path}: {error}") from error
    return parser.document


def _sitemap_urls(data: bytes, label: str) -> set[str]:
    match = _SITEMAP.fullmatch(data)
    if match is None:
        raise AuditError(f"invalid sitemap envelope: {label}")
    body = match.group("body")
    urls: list[str] = []
    position = 0
    for entry in _SITEMAP_ENTRY.finditer(body):
        if body[position : entry.start()].strip():
            raise AuditError(f"invalid sitemap entry shape: {label}")
        location = entry.group("location").decode("ascii")
        lastmod = entry.group("lastmod").decode("ascii")
        if lastmod != NORMALIZATION_DATE:
            raise AuditError(f"invalid normalized sitemap entry: {label}")
        urls.append(location)
        position = entry.end()
    if body[position:].strip() or not urls:
        raise AuditError(f"invalid sitemap entry shape: {label}")
    if len(urls) != len(set(urls)):
        raise AuditError(f"duplicate sitemap URL: {label}")
    return set(urls)


def _local_target(site_dir: Path, path: str, base_path: str) -> Path:
    decoded = unquote(path)
    if not decoded.startswith(base_path):
        raise AuditError(f"same-origin reference leaves project site: {path}")
    relative = decoded[len(base_path) :].lstrip("/")
    candidate = site_dir / relative
    if decoded.endswith("/"):
        candidate /= "index.html"
    resolved = candidate.resolve()
    try:
        resolved.relative_to(site_dir.resolve())
    except ValueError as error:
        raise AuditError(f"site reference escapes output directory: {path}") from error
    return resolved


def _audit_llms(site_dir: Path, content: str, origin: str, base_path: str) -> None:
    canonical = f"Canonical documentation: {origin}"
    if content.count(canonical) != 1:
        raise AuditError(
            "llms.txt does not declare the exact canonical documentation URL"
        )
    routes = re.findall(r"^- (/[^\s]+)$", content, re.MULTILINE)
    if not routes:
        raise AuditError("llms.txt does not advertise any documentation route")
    for route in routes:
        if not _local_target(site_dir, route, base_path).is_file():
            raise AuditError(f"llms.txt route does not exist: {route}")


def audit_site(site_dir: Path, site_url: str) -> tuple[int, int]:
    """Validate generated pages and return page and local-reference counts."""

    site_dir = site_dir.resolve()
    origin = site_url.rstrip("/") + "/"
    origin_parts = urlsplit(origin)
    base_path = origin_parts.path
    if (
        origin_parts.scheme != "https"
        or not origin_parts.netloc
        or not base_path.startswith("/")
        or not base_path.endswith("/")
    ):
        raise AuditError("site URL must be an HTTPS directory URL")

    pages = {
        _route_for_page(path, site_dir): path
        for path in sorted(site_dir.rglob("*.html"))
        if path.name != "404.html"
    }
    if not pages or "" not in pages:
        raise AuditError("generated site has no root page")
    documents = {route: _parse_document(path) for route, path in pages.items()}
    expected_urls = {urljoin(origin, route) for route in pages}

    sitemap = site_dir / "sitemap.xml"
    compressed = site_dir / "sitemap.xml.gz"
    if not sitemap.is_file() or not compressed.is_file():
        raise AuditError("sitemap.xml and sitemap.xml.gz must both exist")
    plain_urls = _sitemap_urls(sitemap.read_bytes(), sitemap.name)
    try:
        compressed_data = gzip.decompress(compressed.read_bytes())
    except (OSError, EOFError) as error:
        raise AuditError("invalid compressed sitemap: sitemap.xml.gz") from error
    if compressed_data != sitemap.read_bytes():
        raise AuditError("compressed sitemap differs from sitemap.xml")
    compressed_urls = _sitemap_urls(compressed_data, compressed.name)
    if plain_urls != expected_urls or compressed_urls != expected_urls:
        raise AuditError(
            "sitemap inventory mismatch; "
            f"plain={sorted(plain_urls)}, gzip={sorted(compressed_urls)}, "
            f"expected={sorted(expected_urls)}"
        )

    llms = site_dir / "llms.txt"
    if (
        not llms.is_file()
        or not (llms_content := llms.read_text(encoding="utf-8")).strip()
    ):
        raise AuditError("llms.txt is missing or empty")
    _audit_llms(site_dir, llms_content, origin, base_path)
    robots = site_dir / "robots.txt"
    if not robots.is_file():
        raise AuditError("robots.txt is missing")
    robots_lines = {
        line.strip()
        for line in robots.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    if not {
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {urljoin(origin, 'sitemap.xml')}",
    }.issubset(robots_lines):
        raise AuditError("robots.txt does not advertise the canonical sitemap")

    checked_references = 0
    for route in pages:
        document = documents[route]
        expected_canonical = urljoin(origin, route)
        if document.canonicals != [expected_canonical]:
            raise AuditError(
                f"{route or '/'}: canonical {document.canonicals!r} "
                f"!= {expected_canonical!r}"
            )
        if document.h1_count != 1:
            raise AuditError(
                f"{route or '/'}: expected one h1, found {document.h1_count}"
            )
        if not "".join(document.title_text).strip():
            raise AuditError(f"{route or '/'}: empty title")

        page_url = urljoin(origin, route)
        for tag, reference in document.references:
            if reference.startswith(("data:", "javascript:", "mailto:", "tel:")):
                continue
            target_url = urlsplit(urljoin(page_url, reference))
            if target_url.scheme == "http":
                raise AuditError(
                    f"{route or '/'}: insecure HTTP reference: {reference}"
                )
            if target_url.scheme not in ("http", "https"):
                continue
            if (target_url.scheme, target_url.netloc) != (
                origin_parts.scheme,
                origin_parts.netloc,
            ):
                continue
            checked_references += 1
            target = _local_target(site_dir, target_url.path, base_path)
            if not target.is_file():
                raise AuditError(
                    f"{route or '/'}: {tag} target does not exist: {reference}"
                )
            if target_url.fragment and target.suffix == ".html":
                target_route = _route_for_page(target, site_dir)
                target_document = documents.get(target_route) or _parse_document(target)
                fragment = unquote(target_url.fragment)
                if fragment not in target_document.ids:
                    raise AuditError(f"{route or '/'}: missing anchor in {reference}")

    for path in site_dir.rglob("*"):
        if path.is_symlink():
            raise AuditError(f"generated site entry is a symlink: {path}")
        if int(path.stat().st_mtime) != NORMALIZATION_EPOCH:
            raise AuditError(f"generated site timestamp differs: {path}")

    return len(pages), checked_references


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", type=Path, default=Path("site"))
    parser.add_argument(
        "--site-url",
        default="https://kogeler.github.io/remote-ssh-mcp-server/",
    )
    arguments = parser.parse_args()
    try:
        pages, references = audit_site(arguments.site_dir, arguments.site_url)
    except (AuditError, OSError) as error:
        print(f"documentation site audit failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(
        f"documentation site audit passed: {pages} pages, {references} local references"
    )


if __name__ == "__main__":
    main()
