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
        "requirements-lint.txt",
        "tools/lint/pyproject.toml",
        "remote_ssh_mcp",
        "doc",
        ".github/workflows/ci.yml",
        ".github/dependabot.yml",
        ".github/CODEOWNERS",
        ".github/actionlint.yaml",
        ".github/scripts/pr-comment.sh",
        ".github/scripts/annotate-diagnostics.sh",
        "tests/live_harness.py",
        "tests/live_support",
        "tests/live_podman_e2e.py",
        "containers/toolbox/Containerfile",
        "containers/toolbox/entrypoint.sh",
        "containers/live-target/Containerfile",
        "containers/live-target/entrypoint.sh",
        "make/container.mk",
        "make/live.mk",
    ):
        assert (root / relative).exists(), relative
    for executable in (
        "remote-ssh-mcp",
        "tests/live_harness.py",
        "containers/toolbox/entrypoint.sh",
        "containers/live-target/entrypoint.sh",
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
    assert "make runtime-venv" in workflow
    assert "make coverage-report" in workflow
    # Ruff runs in its own host venv while project-aware checks stay confined.
    assert "Set up Python for Ruff" in workflow
    assert "cache-dependency-path: requirements-lint.txt" in workflow
    for image in ("toolbox", "resolver", "target"):
        assert f"steps.images.outputs.{image}" in workflow
    assert workflow.count("id: toolbox-cache") == 2
    assert workflow.count("id: resolver-cache") == 1
    assert workflow.count("id: target-cache") == 1
    assert workflow.count("path: .artifacts/images/toolbox.tar") == 2
    assert workflow.count("path: .artifacts/images/resolver.tar") == 1
    assert workflow.count("path: .artifacts/images/live-target.tar") == 1
    assert "${{ runner.arch }}" in workflow
    for target in (
        "image-load-toolbox",
        "image-load-resolver",
        "image-load-target",
        "image-save-toolbox",
        "image-save-resolver",
        "image-save-target",
    ):
        assert f"make {target}" in workflow
    assert "queries: security-extended" in workflow
    assert "comment-summary-in-pr" in workflow
    assert ".github/scripts/pr-comment.sh coverage" in workflow
    assert "RUFF_OUTPUT_FORMAT: github" in workflow
    # Only the two reporting jobs may raise write scope above the read-only
    # workflow default.
    assert workflow.count("pull-requests: write") == 2
    assert workflow.count("runs-on: ubuntu-26.04") == 4
    assert "runs-on: ubuntu-24.04" not in workflow
    # The live isolation must be explicitly rooted in unprivileged Podman.
    assert "Rootless" in workflow
    assert len(references) == workflow.count("uses:")
    assert {name for name, _commit, _version in references} == {
        "actions/cache",
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
        assert "allow" not in update
    python_updates = next(
        update
        for update in configuration["updates"]
        if update["package-ecosystem"] == "pip"
    )
    assert python_updates["ignore"] == [{"dependency-name": "pydantic-core"}]


def test_github_code_owner_covers_the_entire_repository() -> None:
    root = Path(__file__).resolve().parents[1]
    codeowners = (root / ".github/CODEOWNERS").read_text(encoding="utf-8")

    rules = [
        line for line in codeowners.splitlines() if line and not line.startswith("#")
    ]
    assert rules == ["* @kogeler"]


def test_live_topologies_run_confined() -> None:
    root = Path(__file__).resolve().parents[1]
    harness_entrypoint = root / "tests/live_harness.py"
    support_modules = sorted((root / "tests/live_support").glob("*.py"))
    harness = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (harness_entrypoint, *support_modules)
    )
    policy = (root / "make/container.mk").read_text(encoding="utf-8")
    live_makefile = (root / "make/live.mk").read_text(encoding="utf-8")
    entrypoint = (root / "containers/live-target/entrypoint.sh").read_text(
        encoding="utf-8"
    )
    containerfile = (root / "containers/live-target/Containerfile").read_text(
        encoding="utf-8"
    )

    assert len(harness_entrypoint.read_text(encoding="utf-8").splitlines()) <= 20
    assert support_modules
    assert all(
        len(module.read_text(encoding="utf-8").splitlines()) <= 450
        for module in support_modules
    )
    assert "Path(__file__).resolve().parents[2]" in harness
    assert 'TOOL_ROOT / "tests/live_podman_e2e.py"' in harness
    assert "local_root.mkdir(mode=0o700)" in harness
    assert '(local_root / "downloads").mkdir(mode=0o700)' in harness
    assert 'stderr_path = local_root / "server.stderr"' in harness

    # Rootless is the isolation boundary: it is what makes the target's root
    # harmless on the host.
    assert "rootless Podman is required" in harness
    target_policy = policy.split("LIVE_TARGET_CONFINE =", maxsplit=1)[1].split(
        "# A full", maxsplit=1
    )[0]
    assert "--cap-drop=ALL" in target_policy
    # --userns=auto belongs to the toolbox only: sshd's privilege separation
    # cannot call setgroups() inside a narrow auto-allocated map. Match the
    # flag as it would be passed, so the comment explaining its absence does
    # not satisfy the check.
    assert "--userns=" not in target_policy
    for capability in (
        "AUDIT_WRITE",
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "KILL",
        "NET_ADMIN",
        "NET_BIND_SERVICE",
        "SETGID",
        "SETUID",
        "SYS_CHROOT",
    ):
        assert f"--cap-add={capability}" in target_policy

    # Podman rewrites --cap-drop=ALL into a delta against its own default set,
    # so the declared configuration proves nothing and the granted mask is
    # checked instead.
    assert "CapEff" in harness
    for forbidden in (
        "CAP_SYS_ADMIN",
        "CAP_SYS_MODULE",
        "CAP_SYS_PTRACE",
        "CAP_MKNOD",
        "CAP_NET_RAW",
        "CAP_SETFCAP",
    ):
        assert forbidden in harness

    for setting in (
        "--pids-limit=512",
        "--ipc=private",
        "--pid=private",
        "--cgroupns=private",
        "--systemd=false",
        "--log-driver=k8s-file",
    ):
        assert setting in target_policy

    # Automatic live uses the toolbox policy and archive channel, puts both
    # containers on one internal network, and publishes no target port.
    assert "LIVE_SERVER_CONFINE" in policy
    assert "$(filter-out --rm --interactive --network=none,$(BOX_CONFINE))" in policy
    assert "--internal" in harness
    assert "the automatic target published its SSH port" in harness
    assert "live-test: toolbox-image live-target-image" in live_makefile
    assert "$(BOX_ARCHIVE)" in live_makefile
    assert "--server-image $(TOOLBOX_TAG)" in live_makefile
    assert "/usr/local/sbin/toolbox-entrypoint" in harness
    assert "podman cp" not in harness

    # Host keys are generated per container, so no key material can reach an
    # image layer.
    assert "ssh-keygen -A" in entrypoint
    assert "@sha256:" in containerfile
    assert "PasswordAuthentication no" in (
        root / "containers/live-target/sshd.conf"
    ).read_text(encoding="utf-8")


def test_toolbox_runs_confined_and_never_mounts_the_work_tree() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = (root / "make/container.mk").read_text(encoding="utf-8")
    entrypoint = (root / "containers/toolbox/entrypoint.sh").read_text(encoding="utf-8")
    containerfile = (root / "containers/toolbox/Containerfile").read_text(
        encoding="utf-8"
    )

    for flag in (
        "--network=none",
        "--userns=auto:size=2048",
        "--security-opt=no-new-privileges",
        "--cap-drop=ALL",
        "--read-only",
        "--unsetenv-all",
        "--pids-limit=1024",
    ):
        assert flag in policy

    # The work tree is streamed in as a tar. A bind mount would hand the
    # container the host filesystem, the virtual environment, and .git.
    assert "--volume" not in policy
    assert " -v " not in policy
    assert "git ls-files --cached --others --exclude-standard" in policy
    assert '[[ -e "$$path" || -L "$$path" ]]' in policy

    # A container that cannot prove its own confinement does no work.
    for proof in ("CapEff", "NoNewPrivs", "Seccomp", "id -u"):
        assert proof in entrypoint
    assert "BOX_EXPORT_ON_SUCCESS" in entrypoint

    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "tmp/" in ignored
    assert ".artifacts/" in ignored

    container_ignored = (
        (root / ".containerignore").read_text(encoding="utf-8").splitlines()
    )
    for path in (
        ".git/",
        "tmp/",
        "venv/",
        "venv-runtime/",
        "venv-lint/",
        ".artifacts/",
    ):
        assert path in container_ignored

    # Stale disposable networks must be recoverable along with containers.
    assert "network ls --quiet" in policy
    assert "network rm --force" in policy
    assert "LOCK_ONLINE" in policy
    assert "--memory=4g" in policy
    assert "size=512m,size=4g" in policy

    # The resolver cannot bootstrap from its own output, so its complete small
    # dependency tree is pinned inline and restricted to wheels.
    assert "--only-binary=:all:" in containerfile
    for resolver_package in (
        "pip==26.2.1",
        "setuptools==84.0.0",
        "pip-tools==7.6.1",
        "build==1.5.0",
        "click==8.4.2",
        "packaging==26.3",
        "pyproject-hooks==1.2.0",
        "wheel==0.48.0",
    ):
        assert resolver_package in containerfile


def test_container_image_caches_are_independent_and_verified() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = (root / "make/container.mk").read_text(encoding="utf-8")

    image_key = policy.split("image-key:", maxsplit=1)[1].split(
        "# Each archive", maxsplit=1
    )[0]
    for name, key in (
        ("toolbox", "$(TOOLBOX_KEY)"),
        ("resolver", "$(LOCK_KEY)"),
        ("target", "$(TARGET_KEY)"),
    ):
        assert f"{name}=%s" in image_key
        assert key in image_key

    assert (
        "image-save: image-save-toolbox image-save-resolver image-save-target" in policy
    )
    assert (
        "image-load: image-load-toolbox image-load-resolver image-load-target" in policy
    )
    for archive in ("toolbox.tar", "resolver.tar", "live-target.tar"):
        assert f"$(IMAGE_ARTIFACTS)/{archive}" in policy
    assert "cached image archive is missing" in policy
    assert "cached archive did not restore expected image" in policy
    assert ".tmp." in policy


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
    lint_project = tomllib.loads(
        (root / "tools/lint/pyproject.toml").read_text(encoding="utf-8")
    )
    runtime = read_lock(root / "requirements.txt")
    development = read_lock(root / "requirements-dev.txt")
    lint = read_lock(root / "requirements-lint.txt")

    assert project["project"]["version"] == __version__
    direct = project["project"]["dependencies"]
    development_pins = project["project"]["optional-dependencies"]["dev"]
    assert direct == ["mcp==2.0.0", "pydantic==2.13.4"]
    assert development_pins == [
        "bandit[toml]==1.9.4",
        "mypy==2.3.1",
        "packaging==26.3",
        "pip-audit==2.10.1",
        "pytest==9.1.1",
        "pytest-asyncio==1.4.0",
        "pytest-cov==7.1.0",
        "pytest-xdist==3.8.0",
        "pyyaml==6.0.3",
    ]
    assert set(project["project"]["optional-dependencies"]) == {"dev"}

    # Ruff alone may run on the host. A separate project manifest models that
    # environment without folding the server's runtime dependencies into it.
    assert lint_project["project"]["name"] == "remote-ssh-mcp-lint-environment"
    lint_pins = lint_project["project"]["dependencies"]
    assert lint_pins == ["ruff==0.16.3"]

    containerfile = (root / "containers/toolbox/Containerfile").read_text(
        encoding="utf-8"
    )
    for requirement in project["build-system"]["requires"]:
        assert requirement in containerfile

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
    for requirement in lint_pins:
        parsed = Requirement(requirement)
        assert parsed.specifier.contains(lint[canonical(parsed.name)], prereleases=True)

    assert "remote-ssh-mcp" not in runtime
    assert "remote-ssh-mcp" not in development
    assert lint == {"ruff": "0.16.3"}


def test_locks_are_verified_and_split_by_audience() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = read_lock(root / "requirements.txt")
    development = read_lock(root / "requirements-dev.txt")
    lint = read_lock(root / "requirements-lint.txt")

    # The explicit runtime installer uses this lock, so a development tool must
    # never reach an installed server.
    tools = {
        canonical(Requirement(requirement).name)
        for requirement in project["project"]["optional-dependencies"]["dev"]
    }
    assert not tools & set(runtime)

    # A shared transitive package resolved to two versions would make the
    # development environment disagree with what users install.
    assert set(runtime) <= set(development)
    assert all(development[name] == version for name, version in runtime.items())
    assert "ruff" not in development
    assert "pip-tools" not in development
    assert set(lint) == {"ruff"}

    # Every pin carries hashes, which puts pip into hash-checking mode.
    for filename, locked in (
        ("requirements.txt", runtime),
        ("requirements-dev.txt", development),
        ("requirements-lint.txt", lint),
    ):
        lock_text = (root / filename).read_text(encoding="utf-8")
        assert "--hash=sha256:" in lock_text
        for name in locked:
            assert lock_text.count(f"\n{name}==") <= 1
        assert "autogenerated by pip-compile" in lock_text


def test_make_checks_every_lock_and_keeps_live_runtime_only() -> None:
    root = Path(__file__).resolve().parents[1]
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    live_makefile = (root / "make/live.mk").read_text(encoding="utf-8")

    freeze_recipe = makefile.split("freeze-check:", maxsplit=1)[1].split(
        "lint-venv:", maxsplit=1
    )[0]
    for lock in ("$(RUNTIME_LOCK)", "$(DEVELOPMENT_LOCK)", "$(LINT_LOCK)"):
        assert lock in freeze_recipe
    assert "$(LINT_PROJECT)" in freeze_recipe
    assert 'sed "/^[[:space:]]*#/d"' in freeze_recipe
    assert "chmod 0644 $(RUNTIME_LOCK) $(DEVELOPMENT_LOCK) $(LINT_LOCK)" in makefile
    assert "python -m pytest || status=$$?" in makefile
    assert "live-test: toolbox-image live-target-image" in live_makefile
    assert "host-tests: runtime-venv" not in live_makefile
    assert "live-test: runtime-venv" not in live_makefile

    launcher = (root / "remote-ssh-mcp").read_text(encoding="utf-8")
    assert "venv-runtime" in launcher
    assert "make runtime-venv" in launcher
    assert "pip install" not in launcher
    assert "-m venv" not in launcher

    runtime_recipe = makefile.split("runtime-venv:", maxsplit=1)[1].split(
        "# Compiling locks", maxsplit=1
    )[0]
    assert "$(RUNTIME_VENV)" in runtime_recipe
    assert "$(RUNTIME_LOCK)" in runtime_recipe
    assert "--require-hashes" in runtime_recipe
    assert "--only-binary=:all:" in runtime_recipe
    assert "-m pip uninstall --quiet --yes pip" in runtime_recipe
    assert "$(DEVELOPMENT_LOCK)" not in runtime_recipe

    host_recipe = live_makefile.split("host-tests:", maxsplit=1)[1].split(
        "live-preflight:", maxsplit=1
    )[0]
    assert "mktemp -d" in host_recipe
    assert "trap cleanup EXIT" in host_recipe
    assert "$(DEVELOPMENT_LOCK)" in host_recipe
    assert "venv-host-tests" not in makefile
    assert "venv-host-tests" not in live_makefile


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
