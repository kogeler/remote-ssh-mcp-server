SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help
.NOTPARALLEL:

SYSTEM_PYTHON ?= python3
RUNTIME_VENV := venv-runtime
DEV_VENV := venv-dev
LINT_VENV := venv-lint
STANDALONE_VENV := venv-standalone
DOCS_VENV := venv-docs
RUNTIME_PYTHON := $(RUNTIME_VENV)/bin/python
DEV_PYTHON := $(DEV_VENV)/bin/python
LINT_PYTHON := $(LINT_VENV)/bin/python
STANDALONE_PYTHON := $(STANDALONE_VENV)/bin/python
DOCS_PYTHON := $(DOCS_VENV)/bin/python
RUFF := $(LINT_VENV)/bin/ruff
MKDOCS := $(DOCS_VENV)/bin/mkdocs
RUNTIME_INPUT := requirements.in
DEVELOPMENT_INPUT := requirements-dev.in
LINT_INPUT := requirements-lint.in
STANDALONE_INPUT := requirements-standalone.in
DOCS_INPUT := requirements-docs.in
RUNTIME_LOCK := requirements.txt
DEVELOPMENT_LOCK := requirements-dev.txt
LINT_LOCK := requirements-lint.txt
STANDALONE_LOCK := requirements-standalone.txt
DOCS_LOCK := requirements-docs.txt
COMPILE := --quiet --strip-extras --allow-unsafe --generate-hashes \
	--no-emit-find-links --rebuild
LOCK_UPGRADE ?=
VERSION_ARGS ?=
ARTIFACTS := .artifacts
COVERAGE_REPORT := coverage-report.md
COVERAGE_TOTAL := coverage-total.txt
DEPENDENCY_SNAPSHOT := $(ARTIFACTS)/dependency-snapshot.json
RELEASE_NOTES := $(ARTIFACTS)/release-notes.md
NORMALIZATION_EPOCH := 315532800
STANDALONE_ARCH ?=
ANNOTATE := .github/scripts/annotate-diagnostics.sh
SOURCES := remote_ssh_mcp tests tools/container_payload.py \
	tools/audit_docs_site.py tools/build_standalone.py tools/checksums.py \
	tools/smoke_standalone.py \
	tools/standalone_entry.py tools/verify_standalone.py \
	.github/scripts/dependency_audit.py \
	.github/scripts/dependency_snapshot.py .github/scripts/license_policy.py \
	.github/scripts/pr_body.py \
	.github/scripts/version.py \
	doc/site remote-ssh-mcp.py
SHELL_FILES := remote-ssh-mcp containers/toolbox/entrypoint.sh \
	containers/live-target/entrypoint.sh containers/live-target/install-base.sh \
	$(ANNOTATE)
ACTIONLINT_IMAGE := docker.io/rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

include make/container.mk
include make/live.mk

.PHONY: help runtime-venv dev-venv lint-venv standalone-venv docs-venv lock \
	refresh-dependencies freeze-check format format-check lint type-check bandit \
	test host-tests package standalone smoke-standalone checksums \
	docs-build docs-audit docs-serve \
	test-network-block confinement-test coverage-report syntax \
	shellcheck version-check version-sync release-notes dependency-snapshot \
	compatibility-python audit audit-raw licenses outdated \
	validator-image-actionlint validate-actions check ci clean

help:
	@printf '%s\n' \
		'make runtime-venv  Install the explicit runtime environment' \
		'make lock          Regenerate all five exact hash locks' \
		'make refresh-dependencies  Upgrade and regenerate all locks' \
		'make format        Apply Ruff fixes and formatting' \
		'make test          Run the ordinary parallel test suite' \
		'make package       Build and install-test wheel and source archive' \
		'make standalone    Build and validate this host architecture binary' \
		'make smoke-standalone  Exercise the native binary outside the source tree' \
		'make docs-audit    Render and audit the documentation site' \
		'make docs-serve    Serve the documentation site locally' \
		'make host-tests    Run explicitly marked host tests' \
		'make audit         Enforce reviewed vulnerability policy' \
		'make licenses      Enforce license policy across all five locks' \
		'make dependency-snapshot  Render the five lock manifests' \
		'make version-check Verify exact release metadata' \
		'make release-notes Render notes for the current version' \
		'make images        Build the project development and live images' \
		'make live-test     Run the automatic ephemeral-key MCP live matrix' \
		'make live-fido-test PUBLIC_KEY=... IDENTITY_FILE=...' \
		'                  Run the operator-controlled hardware-key matrix' \
		'make check         Run tests, linters, packaging, policy, and compatibility' \
		'make ci            Run make check plus the vulnerability audit'

runtime-venv:
	@if [[ ! -x '$(RUNTIME_PYTHON)' ]] || \
		[[ ! -f '$(RUNTIME_VENV)/.requirements.txt' ]] || \
		[[ ! -f '$(RUNTIME_VENV)/.version' ]] || \
		! cmp -s requirements.txt '$(RUNTIME_VENV)/.requirements.txt' || \
		! cmp -s .version '$(RUNTIME_VENV)/.version' || \
		! '$(RUNTIME_PYTHON)' -c 'import importlib.metadata, pathlib, remote_ssh_mcp, ssh_wrapper; expected = pathlib.Path(".version").read_text(encoding="utf-8").strip(); assert remote_ssh_mcp.__version__ == expected; assert importlib.metadata.version("remote-ssh-mcp") == expected; assert importlib.metadata.version("ssh-wrapper") == "0.1.0"' >/dev/null 2>&1 || \
		'$(RUNTIME_PYTHON)' -c 'import pip' >/dev/null 2>&1; then \
		if [[ -e '$(RUNTIME_VENV)' ]]; then \
			find '$(RUNTIME_VENV)' -depth -delete; \
		fi; \
		'$(SYSTEM_PYTHON)' -m venv '$(RUNTIME_VENV)'; \
		'$(RUNTIME_PYTHON)' -m pip install --quiet --require-hashes \
			--only-binary=:all: \
			--requirement requirements.txt; \
		'$(RUNTIME_PYTHON)' -m pip install --quiet --no-deps --editable .; \
		'$(RUNTIME_PYTHON)' -m pip check; \
		'$(RUNTIME_PYTHON)' -m pip uninstall --quiet --yes pip; \
		cp -- requirements.txt '$(RUNTIME_VENV)/.requirements.txt'; \
		cp -- .version '$(RUNTIME_VENV)/.version'; \
	fi

dev-venv:
	@if [[ ! -x '$(DEV_PYTHON)' ]] || \
		[[ ! -f '$(DEV_VENV)/.requirements-dev.txt' ]] || \
		! cmp -s requirements-dev.txt '$(DEV_VENV)/.requirements-dev.txt' || \
		! '$(DEV_PYTHON)' -c 'import importlib.metadata, pip, ssh_wrapper; assert importlib.metadata.version("ssh-wrapper") == "0.1.0"' >/dev/null 2>&1; then \
		if [[ -e '$(DEV_VENV)' ]]; then find '$(DEV_VENV)' -depth -delete; fi; \
		'$(SYSTEM_PYTHON)' -m venv '$(DEV_VENV)'; \
		'$(DEV_PYTHON)' -m pip install --quiet --require-hashes \
			--only-binary=:all: \
			--requirement requirements-dev.txt; \
		cp -- requirements-dev.txt '$(DEV_VENV)/.requirements-dev.txt'; \
	fi
	@$(DEV_PYTHON) -m pip install --quiet --no-deps --editable .
	@$(DEV_PYTHON) -m pip check

standalone-venv:
	@if [[ ! -x '$(STANDALONE_PYTHON)' ]] || \
		[[ ! -f '$(STANDALONE_VENV)/.requirements-standalone.txt' ]] || \
		! cmp -s '$(STANDALONE_LOCK)' \
			'$(STANDALONE_VENV)/.requirements-standalone.txt' || \
		! '$(STANDALONE_PYTHON)' -c \
			'import PyInstaller, mcp, pip, pydantic, ssh_wrapper' \
			>/dev/null 2>&1; then \
		if [[ -e '$(STANDALONE_VENV)' ]]; then \
			find '$(STANDALONE_VENV)' -depth -delete; \
		fi; \
		'$(SYSTEM_PYTHON)' -m venv '$(STANDALONE_VENV)'; \
		'$(STANDALONE_PYTHON)' -m pip install --quiet --require-hashes \
			--only-binary=:all: --requirement '$(STANDALONE_LOCK)'; \
		cp -- '$(STANDALONE_LOCK)' \
			'$(STANDALONE_VENV)/.requirements-standalone.txt'; \
	fi
	@$(STANDALONE_PYTHON) -m pip check

lock: lock-image
	@$(BOX_ARCHIVE) | $(PODMAN) run $(LOCK_ONLINE) \
		--env BOX_EXPORT="$(RUNTIME_LOCK) $(DEVELOPMENT_LOCK) $(LINT_LOCK) $(STANDALONE_LOCK) $(DOCS_LOCK)" \
		--env BOX_EXPORT_ON_SUCCESS=1 $(LOCK_TAG) sh -ceu \
		'python -m piptools compile $(COMPILE) $(LOCK_UPGRADE) \
			--output-file=$(RUNTIME_LOCK) $(RUNTIME_INPUT); \
		python -m piptools compile $(COMPILE) $(LOCK_UPGRADE) \
			--output-file=$(DEVELOPMENT_LOCK) $(DEVELOPMENT_INPUT); \
		python -m piptools compile $(COMPILE) $(LOCK_UPGRADE) \
			--output-file=$(LINT_LOCK) $(LINT_INPUT); \
		python -m piptools compile $(COMPILE) $(LOCK_UPGRADE) \
			--output-file=$(STANDALONE_LOCK) $(STANDALONE_INPUT); \
		python -m piptools compile $(COMPILE) $(LOCK_UPGRADE) \
			--output-file=$(DOCS_LOCK) $(DOCS_INPUT); \
		chmod 0644 $(RUNTIME_LOCK) $(DEVELOPMENT_LOCK) $(LINT_LOCK) \
			$(STANDALONE_LOCK) $(DOCS_LOCK)' \
		| $(PAYLOAD_MERGE)

refresh-dependencies:
	@$(MAKE) lock LOCK_UPGRADE=--upgrade
	@for path in $(RUNTIME_VENV) $(DEV_VENV) $(LINT_VENV) $(STANDALONE_VENV) \
		$(DOCS_VENV); do \
		if [[ -e "$$path" ]]; then find "$$path" -depth -delete; fi; \
	done

freeze-check: lock-image
	@$(BOX_ARCHIVE) | $(PODMAN) run $(LOCK_ONLINE) $(LOCK_TAG) bash -ceu \
		'python -m piptools compile $(COMPILE) \
			--constraint=$(RUNTIME_LOCK) --output-file=/tmp/runtime.txt \
			$(RUNTIME_INPUT); \
		python -m piptools compile $(COMPILE) \
			--constraint=$(DEVELOPMENT_LOCK) \
			--output-file=/tmp/development.txt $(DEVELOPMENT_INPUT); \
		python -m piptools compile $(COMPILE) \
			--constraint=$(LINT_LOCK) --output-file=/tmp/lint.txt \
			$(LINT_INPUT); \
		python -m piptools compile $(COMPILE) \
			--constraint=$(STANDALONE_LOCK) \
			--output-file=/tmp/standalone.txt $(STANDALONE_INPUT); \
		python -m piptools compile $(COMPILE) \
			--constraint=$(DOCS_LOCK) \
			--output-file=/tmp/docs.txt $(DOCS_INPUT); \
		diff -u <(sed "/^[[:space:]]*#/d" $(RUNTIME_LOCK)) \
			<(sed "/^[[:space:]]*#/d" /tmp/runtime.txt); \
		diff -u <(sed "/^[[:space:]]*#/d" $(DEVELOPMENT_LOCK)) \
			<(sed "/^[[:space:]]*#/d" /tmp/development.txt); \
		diff -u <(sed "/^[[:space:]]*#/d" $(LINT_LOCK)) \
			<(sed "/^[[:space:]]*#/d" /tmp/lint.txt); \
		diff -u <(sed "/^[[:space:]]*#/d" $(STANDALONE_LOCK)) \
			<(sed "/^[[:space:]]*#/d" /tmp/standalone.txt); \
		diff -u <(sed "/^[[:space:]]*#/d" $(DOCS_LOCK)) \
			<(sed "/^[[:space:]]*#/d" /tmp/docs.txt)'

lint-venv:
	@if [[ ! -x '$(RUFF)' ]] || \
		[[ ! -f '$(LINT_VENV)/.requirements-lint.txt' ]] || \
		! cmp -s requirements-lint.txt '$(LINT_VENV)/.requirements-lint.txt'; then \
		if [[ -e '$(LINT_VENV)' ]]; then find '$(LINT_VENV)' -depth -delete; fi; \
		'$(SYSTEM_PYTHON)' -m venv '$(LINT_VENV)'; \
		'$(LINT_PYTHON)' -m pip install --quiet --require-hashes \
			--only-binary=:all: --requirement requirements-lint.txt; \
		cp -- requirements-lint.txt '$(LINT_VENV)/.requirements-lint.txt'; \
	fi

docs-venv:
	@if [[ ! -x '$(MKDOCS)' ]] || \
		[[ ! -f '$(DOCS_VENV)/.requirements-docs.txt' ]] || \
		! cmp -s '$(DOCS_LOCK)' '$(DOCS_VENV)/.requirements-docs.txt' || \
		! '$(DOCS_PYTHON)' -c 'import mkdocs, pip' >/dev/null 2>&1; then \
		if [[ -e '$(DOCS_VENV)' ]]; then find '$(DOCS_VENV)' -depth -delete; fi; \
		'$(SYSTEM_PYTHON)' -m venv '$(DOCS_VENV)'; \
		'$(DOCS_PYTHON)' -m pip install --quiet --require-hashes \
			--only-binary=:all: --requirement '$(DOCS_LOCK)'; \
		cp -- '$(DOCS_LOCK)' '$(DOCS_VENV)/.requirements-docs.txt'; \
	fi

docs-build: docs-venv
	@$(MKDOCS) build --strict --clean

docs-audit: docs-build
	@$(SYSTEM_PYTHON) tools/audit_docs_site.py \
		--site-dir site \
		--site-url 'https://kogeler.github.io/remote-ssh-mcp-server/'

docs-serve: docs-venv
	@$(MKDOCS) serve --strict

format: lint-venv
	@$(RUFF) check --fix $(SOURCES)
	@$(RUFF) format $(SOURCES)

format-check: lint-venv
	@$(RUFF) format --check $(SOURCES)

lint: lint-venv
	@$(RUFF) check $(SOURCES)

type-check: dev-venv
	@set -o pipefail; $(DEV_PYTHON) -m mypy | bash $(ANNOTATE) mypy

bandit: dev-venv
	@$(DEV_PYTHON) -m bandit -q -c pyproject.toml -r \
		remote_ssh_mcp remote-ssh-mcp.py tools/audit_docs_site.py \
		doc/site/hooks.py .github/scripts/pr_body.py

test: dev-venv
	@mkdir -p $(ARTIFACTS)
	@$(DEV_PYTHON) -m pytest
	@$(DEV_PYTHON) -m coverage report --format=markdown \
		> $(ARTIFACTS)/$(COVERAGE_REPORT)
	@$(DEV_PYTHON) -m coverage report --format=total \
		> $(ARTIFACTS)/$(COVERAGE_TOTAL)
	@cp -- coverage.xml $(ARTIFACTS)/coverage.xml

host-tests: dev-venv
	@$(DEV_PYTHON) -m pytest -m host --no-cov

package: dev-venv
	@if [[ -e '$(ARTIFACTS)/dist' ]]; then \
		find '$(ARTIFACTS)/dist' -depth -delete; \
	fi
	@if [[ -e '$(ARTIFACTS)/package-install' ]]; then \
		find '$(ARTIFACTS)/package-install' -depth -delete; \
	fi
	@mkdir -p '$(ARTIFACTS)/dist' '$(ARTIFACTS)/package-install'
	@$(DEV_PYTHON) -m build --no-isolation --wheel --sdist \
		--outdir '$(ARTIFACTS)/dist' .
	@mapfile -t wheels < <(find '$(ARTIFACTS)/dist' -maxdepth 1 \
		-type f -name '*.whl' -print); \
		mapfile -t sdists < <(find '$(ARTIFACTS)/dist' -maxdepth 1 \
		-type f -name '*.tar.gz' -print); \
		[[ $${#wheels[@]} -eq 1 && $${#sdists[@]} -eq 1 ]] || { \
			printf 'expected exactly one wheel and one sdist\n' >&2; exit 1; \
		}; \
		'$(DEV_PYTHON)' -m pip install --quiet --no-deps \
			--target '$(ARTIFACTS)/package-install' "$${wheels[0]}"; \
		install_root=$$(realpath '$(ARTIFACTS)/package-install'); \
		entry_point="$$install_root/bin/remote-ssh-mcp"; \
		test -x "$$entry_point"; \
		cd /tmp; \
		PYTHONPATH="$$install_root" '$(CURDIR)/$(DEV_PYTHON)' -c \
			'import pathlib, remote_ssh_mcp; assert pathlib.Path(remote_ssh_mcp.__file__).is_relative_to(pathlib.Path("'"$$install_root"'"))'; \
		EXPECTED_ROOT='$(CURDIR)' PYTHONPATH="$$install_root" \
			'$(CURDIR)/$(DEV_PYTHON)' -c \
			'import os, pathlib; from remote_ssh_mcp.config import runtime_repository_root; assert runtime_repository_root() == pathlib.Path(os.environ["EXPECTED_ROOT"]).resolve()'; \
		PYTHONPATH="$$install_root" "$$entry_point" --help >/dev/null; \
		if PYTHONPATH="$$install_root" "$$entry_point" --connect-timeout 0 \
			>"$$install_root/operational.stdout" \
			2>"$$install_root/operational.stderr"; then \
			printf 'invalid operational probe unexpectedly passed\n' >&2; exit 1; \
		fi; \
		grep -F 'connect timeout must be between' \
			"$$install_root/operational.stderr" >/dev/null; \
		cd '$(CURDIR)'; \
		for path in build remote_ssh_mcp.egg-info; do \
			if [[ -e "$$path" ]]; then find "$$path" -depth -delete; fi; \
		done

standalone: standalone-venv
	@arch=$$($(STANDALONE_PYTHON) -c \
		'from tools.build_standalone import standalone_architecture; print(standalone_architecture())'); \
	requested='$(STANDALONE_ARCH)'; \
	if [[ -n "$$requested" && "$$requested" != "$$arch" ]]; then \
		printf 'Native architecture %s does not match STANDALONE_ARCH=%s\n' \
			"$$arch" "$$requested" >&2; exit 1; \
	fi; \
	$(STANDALONE_PYTHON) tools/build_standalone.py \
		--python '$(STANDALONE_PYTHON)' --output dist \
		--epoch '$(NORMALIZATION_EPOCH)' --expected-architecture "$$arch"; \
	$(SYSTEM_PYTHON) tools/verify_standalone.py \
		"dist/remote-ssh-mcp-linux-$$arch" \
		--provenance "$(ARTIFACTS)/standalone-provenance-$$arch.json" \
		--architecture "$$arch" --epoch '$(NORMALIZATION_EPOCH)'

smoke-standalone: standalone
	@arch=$$($(STANDALONE_PYTHON) -c \
		'from tools.build_standalone import standalone_architecture; print(standalone_architecture())'); \
	$(SYSTEM_PYTHON) tools/smoke_standalone.py \
		"dist/remote-ssh-mcp-linux-$$arch"

checksums:
	@$(SYSTEM_PYTHON) tools/checksums.py --directory dist

test-network-block: toolbox-image
	$(BOX_RUN) python -c 'import socket; sock = socket.socket(); \
		sock.settimeout(0.2); raise SystemExit(sock.connect_ex(("1.1.1.1", 443)) == 0)'

confinement-test: toolbox-image
	$(BOX_RUN) sh -ceu 'test "$$(id -u)" -ne 0; \
		grep -Eq "^CapEff:[[:space:]]+0+$$" /proc/self/status; \
		grep -Eq "^NoNewPrivs:[[:space:]]+1$$" /proc/self/status; \
		grep -Eq "^Seccomp:[[:space:]]+2$$" /proc/self/status; \
		test ! -e .git; test ! -S /run/podman/podman.sock; \
		test ! -S /var/run/docker.sock; \
		test -z "$${SSH_AUTH_SOCK:-}"; test -z "$${GITHUB_TOKEN:-}"; \
		test -z "$${GH_TOKEN:-}"; test -z "$${CONTAINER_HOST:-}"; \
		test -z "$${DOCKER_HOST:-}"; \
		! touch /etc/remote-ssh-mcp-toolbox-write 2>/dev/null; \
		touch /tmp/toolbox-write /work/toolbox-write; \
		touch remote_ssh_mcp/.container-mutation'
	@test ! -e remote_ssh_mcp/.container-mutation

coverage-report:
	@if [[ ! -f $(ARTIFACTS)/$(COVERAGE_TOTAL) ]]; then \
		printf '%s\n' 'No coverage data; run make test first.' >&2; exit 1; \
	fi
	@total=$$(cat -- $(ARTIFACTS)/$(COVERAGE_TOTAL)); \
		threshold=$$($(SYSTEM_PYTHON) -c \
			'import pathlib,tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["tool"]["coverage"]["report"]["fail_under"])'); \
		printf '### Coverage: %s%% (threshold %s%%)\n\n' \
			"$$total" "$$threshold"; \
		cat -- $(ARTIFACTS)/$(COVERAGE_REPORT)

syntax:
	@$(SYSTEM_PYTHON) -m compileall -q $(SOURCES)
	@bash -n $(SHELL_FILES)

shellcheck: toolbox-image
	$(BOX_RUN) shellcheck $(SHELL_FILES)

version-check:
	@$(SYSTEM_PYTHON) .github/scripts/version.py check $(VERSION_ARGS)

version-sync:
	@$(SYSTEM_PYTHON) .github/scripts/version.py sync

release-notes:
	@mkdir -p $(ARTIFACTS)
	@$(SYSTEM_PYTHON) .github/scripts/version.py notes --output $(RELEASE_NOTES)

dependency-snapshot:
	@mkdir -p $(ARTIFACTS)
	@$(SYSTEM_PYTHON) .github/scripts/dependency_snapshot.py \
		--output $(DEPENDENCY_SNAPSHOT)

compatibility-python: compatibility-image
	@mkdir -p $(ARTIFACTS)/compatibility
	@$(BOX_ARCHIVE) | $(PODMAN) run $(COMPATIBILITY_ONLINE) \
		--env BOX_EXPORT="$(ARTIFACTS)/compatibility/python313-resolved.txt" \
		--env BOX_EXPORT_ON_SUCCESS=1 $(COMPATIBILITY_TAG) sh -ceu \
		'mkdir -p $(ARTIFACTS)/compatibility; \
		python -m piptools compile $(COMPILE) \
			--output-file=$(ARTIFACTS)/compatibility/python313-resolved.txt \
			$(DEVELOPMENT_INPUT); \
		python -m venv /tmp/python313-tests; \
		/tmp/python313-tests/bin/python -m pip install --quiet --require-hashes \
			--only-binary=:all: \
			--requirement $(ARTIFACTS)/compatibility/python313-resolved.txt; \
		/tmp/python313-tests/bin/python -m pip install --quiet --no-deps \
			--editable .; \
		/tmp/python313-tests/bin/python -m pip check; \
		/tmp/python313-tests/bin/python -m pytest' | $(PAYLOAD_MERGE)

audit: dev-venv
	@$(DEV_PYTHON) .github/scripts/dependency_audit.py

audit-raw: dev-venv
	@$(DEV_PYTHON) -m pip_audit --local --strict

licenses: runtime-venv dev-venv lint-venv standalone-venv docs-venv
	@$(DEV_PYTHON) .github/scripts/license_policy.py

outdated: dev-venv
	@$(DEV_PYTHON) -m pip list --outdated

validator-image-actionlint:
	@$(PODMAN) image exists '$(ACTIONLINT_TAG)' || { \
		printf 'building %s\n' '$(ACTIONLINT_TAG)' >&2; \
		$(BOX_ARCHIVE) | $(PODMAN) build --quiet --pull=missing \
			--build-arg ACTIONLINT_IMAGE='$(ACTIONLINT_IMAGE)' \
			--tag '$(ACTIONLINT_TAG)' \
			--file containers/actionlint/Containerfile - >/dev/null; \
	}

validate-actions: validator-image-actionlint
	@$(BOX_ARCHIVE) | $(PODMAN) run $(BOX_CONFINE) $(ACTIONLINT_TAG) \
		actionlint -no-color -config-file .github/actionlint.yaml \
		.github/workflows/*.yml

check: lint format-check type-check bandit test package docs-audit test-network-block \
	confinement-test syntax shellcheck freeze-check dependency-snapshot \
	version-check compatibility-python licenses validate-actions

ci: check audit

clean:
	@find remote_ssh_mcp tests tools doc/site .github/scripts -type f \
		-path '*/__pycache__/*' -delete
	@find remote_ssh_mcp tests tools doc/site .github/scripts -depth -type d \
		-name __pycache__ -empty -delete
	@for path in '$(RUNTIME_VENV)' '$(DEV_VENV)' '$(LINT_VENV)' \
		'$(STANDALONE_VENV)' '$(DOCS_VENV)' \
		.pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml .artifacts \
		__pycache__ build dist site remote_ssh_mcp.egg-info; do \
		if [[ -e "$$path" ]]; then find "$$path" -depth -delete; fi; \
	done
