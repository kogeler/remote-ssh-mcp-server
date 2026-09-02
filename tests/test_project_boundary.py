# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Regression tests for the independent repository boundary."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

import remote_ssh_mcp

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "remote_ssh_mcp"
RUNTIME_IMPORTS = {"mcp", "pydantic", "ssh_wrapper"}


def test_version_metadata_is_synchronized() -> None:
    version = (ROOT / ".version").read_text(encoding="utf-8").strip()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
    assert project["project"]["name"] == "remote-ssh-mcp"
    assert project["project"]["version"] == version
    assert remote_ssh_mcp.__version__ == version
    assert f"## {version} - " in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_runtime_imports_only_declared_or_standard_modules() -> None:
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported = tuple(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert node.module is not None
                imported = (node.module.partition(".")[0],)
            assert all(
                name in sys.stdlib_module_names or name in RUNTIME_IMPORTS
                for name in imported
            ), (path, imported)


def test_local_documentation_links_are_relative_and_resolve() -> None:
    link_pattern = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
    documents = (ROOT / "README.md", *sorted((ROOT / "doc").rglob("*.md")))

    for document in documents:
        for raw_target in link_pattern.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme in {"http", "https", "mailto"}:
                continue
            assert not parsed.scheme and not target.startswith("/"), (document, target)
            relative = unquote(parsed.path)
            if not relative:
                continue
            destination = (document.parent / relative).resolve()
            assert destination.is_relative_to(ROOT.resolve()), (document, raw_target)
            assert destination.exists(), (document, raw_target)


def test_project_contains_no_legacy_core_residue() -> None:
    forbidden = (
        "remote_ssh_" + "core",
        "remote-ssh-" + "core",
    )
    ignored = {
        ".artifacts",
        ".coverage",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
    }
    ignored_prefixes = ("venv-",)
    text_suffixes = {
        "",
        ".in",
        ".json",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".yml",
    }

    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if not path.is_file() or any(
            part in ignored or part.startswith(ignored_prefixes)
            for part in relative.parts
        ):
            continue
        if path.name == "LICENSE" or path.suffix in text_suffixes:
            content = path.read_text(encoding="utf-8").casefold()
            assert not any(marker in content for marker in forbidden), path


def test_repository_contains_only_the_mcp_product_tree() -> None:
    """Previously extracted sibling products cannot return as local sources."""
    source_entries = {
        ".github",
        ".gitignore",
        ".version",
        "AGENTS.md",
        "CHANGELOG.md",
        "LICENSE",
        "Makefile",
        "README.md",
        "containers",
        "doc",
        "make",
        "mkdocs.yml",
        "pyproject.toml",
        "pytest.ini",
        "remote-ssh-mcp",
        "remote-ssh-mcp.py",
        "remote_ssh_mcp",
        "requirements-dev.in",
        "requirements-dev.txt",
        "requirements-docs.in",
        "requirements-docs.txt",
        "requirements-lint.in",
        "requirements-lint.txt",
        "requirements-standalone.in",
        "requirements-standalone.txt",
        "requirements.in",
        "requirements.txt",
        "ruff.toml",
        "tests",
        "tools",
    }
    generated_entries = {
        ".artifacts",
        ".coverage",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".remote-ssh-mcp",
        ".ruff_cache",
        "__pycache__",
        "build",
        "coverage.xml",
        "dist",
        "htmlcov",
        "remote_ssh_mcp.egg-info",
        "venv-dev",
        "venv-docs",
        "venv-lint",
        "venv-runtime",
        "venv-standalone",
        "site",
    }
    actual = {path.name for path in ROOT.iterdir()}
    unexpected = actual - source_entries - generated_entries
    assert not {name for name in unexpected if not name.startswith(".coverage.")}, (
        unexpected
    )
    assert source_entries <= actual
    assert not (ROOT / "remote_ssh_mcp/pyproject.toml").exists()
