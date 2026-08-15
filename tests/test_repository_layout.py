from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement

from remote_ssh_mcp import __version__

MARKDOWN_LINK = re.compile(r"\[[^]]*]\(([^)]+)\)")
LOCK_PIN = re.compile(r"^([A-Za-z0-9._-]+)==([^\s\\]+)")
ACTION_REFERENCE = re.compile(
    r"^\s*uses:\s*([^@\s]+)@([0-9a-f]{40})\s+#\s+(v[0-9][^\s]*)$",
    re.MULTILINE,
)


def test_public_repository_is_self_contained() -> None:
    root = Path(__file__).resolve().parents[1]

    for relative in (
        "remote-ssh-mcp",
        "remote-ssh-mcp.py",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "remote_ssh_mcp",
        "doc",
        ".github/workflows/ci.yml",
        ".github/dependabot.yml",
        ".github/CODEOWNERS",
        ".github/actionlint.yaml",
        ".github/scripts/pr-comment.sh",
        ".github/scripts/annotate-diagnostics.sh",
        "tests/run-live-lxc.sh",
        "tests/run-live-fido-lxc.sh",
        "tests/run-live-lxc-core.sh",
    ):
        assert (root / relative).exists(), relative
    for executable in (
        "remote-ssh-mcp",
        ".github/scripts/pr-comment.sh",
        ".github/scripts/annotate-diagnostics.sh",
    ):
        assert (root / executable).stat().st_mode & 0o111, executable


def test_github_ci_triggers_main_and_pins_every_action() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    references = ACTION_REFERENCE.findall(workflow)

    assert "push:\n    branches:\n      - main" in workflow
    assert "pull_request:\n    branches:\n      - main" in workflow
    assert "make ci" in workflow
    assert "make live-test" in workflow
    assert "make coverage-report" in workflow
    assert "queries: security-extended" in workflow
    assert "comment-summary-in-pr" in workflow
    assert ".github/scripts/pr-comment.sh coverage coverage-report.md" in workflow
    assert "RUFF_OUTPUT_FORMAT: github" in workflow
    # Only the two reporting jobs may raise write scope above the read-only
    # workflow default.
    assert workflow.count("pull-requests: write") == 2
    assert workflow.count("runs-on: ubuntu-26.04") == 4
    assert "runs-on: ubuntu-24.04" not in workflow
    assert "sudo snap install lxd\n" in workflow
    assert "snap install lxd --channel" not in workflow
    # The runner ships Docker, which blocks forwarded LXD bridge traffic.
    assert "DOCKER-USER" in workflow
    assert len(references) == workflow.count("uses:")
    assert {name for name, _commit, _version in references} == {
        "actions/checkout",
        "actions/dependency-review-action",
        "actions/setup-python",
        "github/codeql-action/analyze",
        "github/codeql-action/init",
    }


def test_every_workflow_pins_every_action() -> None:
    root = Path(__file__).resolve().parents[1]
    workflows = sorted((root / ".github/workflows").glob("*.yml"))

    assert workflows, "no workflow files found"
    for path in workflows:
        workflow = path.read_text(encoding="utf-8")
        references = ACTION_REFERENCE.findall(workflow)
        assert len(references) == workflow.count("uses:"), path.name


def test_dependabot_watches_both_ecosystems() -> None:
    root = Path(__file__).resolve().parents[1]
    configuration = yaml.safe_load(
        (root / ".github/dependabot.yml").read_text(encoding="utf-8")
    )

    ecosystems = {update["package-ecosystem"] for update in configuration["updates"]}
    assert ecosystems == {"pip", "github-actions"}
    for update in configuration["updates"]:
        assert update["schedule"]["interval"] == "weekly"
        # A restricted allow list belongs to hand-written requirement files.
        # pip-compile output is recompiled as a whole, so nothing needs to be
        # withheld from Dependabot here.
        assert "allow" not in update


def test_github_code_owner_covers_the_entire_repository() -> None:
    root = Path(__file__).resolve().parents[1]
    codeowners = (root / ".github/CODEOWNERS").read_text(encoding="utf-8")

    rules = [
        line for line in codeowners.splitlines() if line and not line.startswith("#")
    ]
    assert rules == ["* @kogeler"]


def test_live_wrappers_share_the_hardened_lxc_core() -> None:
    root = Path(__file__).resolve().parents[1]
    automatic = (root / "tests/run-live-lxc.sh").read_text(encoding="utf-8")
    fido = (root / "tests/run-live-fido-lxc.sh").read_text(encoding="utf-8")
    core = (root / "tests/run-live-lxc-core.sh").read_text(encoding="utf-8")

    assert "run-live-lxc-core.sh" in automatic
    assert "run-live-lxc-core.sh" in fido
    for setting in (
        "security.nesting=true",
        "security.syscalls.intercept.mknod=true",
        "security.syscalls.intercept.setxattr=true",
        "security.idmap.size=1000000",
        "security.devlxd=false",
        "security.idmap.isolated=true",
        "linux.kernel_modules=br_netfilter",
        "security.mac_filtering=true",
        "security.ipv4_filtering=true",
        "security.ipv6_filtering=true",
    ):
        assert core.count(setting) >= 2
    assert core.index('lxc config device override "$container_name" eth0') < core.index(
        'lxc start "$container_name"'
    )


def read_lock(path: Path) -> dict[str, str]:
    """Map every pinned name in pip-compile output to its version."""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LOCK_PIN.match(line)
        if match:
            pins[canonical(match.group(1))] = match.group(2)
    return pins


def canonical(name: str) -> str:
    return name.casefold().replace("_", "-")


def test_project_metadata_declares_only_direct_dependencies() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = read_lock(root / "requirements.txt")
    development = read_lock(root / "requirements-dev.txt")

    assert project["project"]["version"] == __version__
    direct = project["project"]["dependencies"]
    development_pins = project["project"]["optional-dependencies"]["dev"]
    assert direct == ["mcp==2.0.0", "pydantic==2.13.4"]
    assert development_pins == [
        "bandit[toml]==1.9.4",
        "mypy==2.3.1",
        "pip-audit==2.10.1",
        "pip-tools==7.6.1",
        "pytest==9.1.1",
        "pytest-asyncio==1.4.0",
        "pytest-cov==7.1.0",
        "pytest-xdist==3.8.0",
        "ruff==0.16.3",
    ]

    for requirement in direct:
        parsed = Requirement(requirement)
        assert parsed.specifier.contains(
            runtime[canonical(parsed.name)], prereleases=True
        )
    for requirement in (*direct, *development_pins):
        parsed = Requirement(requirement)
        assert parsed.specifier.contains(
            development[canonical(parsed.name)], prereleases=True
        )

    assert "remote-ssh-mcp" not in runtime
    assert "remote-ssh-mcp" not in development


def test_locks_are_verified_and_split_by_audience() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    runtime_text = (root / "requirements.txt").read_text(encoding="utf-8")
    runtime = read_lock(root / "requirements.txt")
    development = read_lock(root / "requirements-dev.txt")

    # The launcher installs the runtime lock, so a development tool must never
    # reach an installed server.
    tools = {
        canonical(Requirement(requirement).name)
        for requirement in project["project"]["optional-dependencies"]["dev"]
    }
    assert not tools & set(runtime)

    # A shared transitive package resolved to two versions would make the
    # development environment disagree with what users install.
    assert set(runtime) <= set(development)
    assert all(development[name] == version for name, version in runtime.items())

    # Every pin carries hashes, which puts pip into hash-checking mode.
    assert "--hash=sha256:" in runtime_text
    for name in runtime:
        assert runtime_text.count(f"\n{name}==") <= 1
    assert "autogenerated by pip-compile" in runtime_text


def test_coverage_gate_is_configured_in_one_place() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = (root / "pytest.ini").read_text(encoding="utf-8")
    makefile = (root / "Makefile").read_text(encoding="utf-8")

    coverage = project["tool"]["coverage"]
    assert coverage["run"]["source"] == ["remote_ssh_mcp"]
    assert coverage["run"]["branch"] is True
    assert coverage["report"]["fail_under"] >= 75
    # The default pytest run measures coverage, so make check and make ci fail
    # on a regression without a separate target.
    assert "--cov" in pytest_options
    # The Makefile must read the threshold instead of repeating it.
    assert str(coverage["report"]["fail_under"]) not in makefile


def test_local_documentation_links_resolve() -> None:
    root = Path(__file__).resolve().parents[1]

    for document in (root / "README.md", *(root / "doc").glob("*.md")):
        for target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            path_text = target.split("#", maxsplit=1)[0]
            assert (document.parent / path_text).exists(), (
                f"broken link in {document.relative_to(root)}: {target}"
            )
