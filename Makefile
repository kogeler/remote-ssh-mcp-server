SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

# Ruff is the only persistent host-side development tool: one self-contained
# binary with no dependencies, also used by editors for format-on-save. The
# separately installed runtime is for running the MCP server, not development.
RUNTIME_VENV := venv-runtime
RUNTIME_PYTHON := $(RUNTIME_VENV)/bin/python
RUNTIME_STATE := $(RUNTIME_VENV)/.requirements.txt
LINT_VENV := venv-lint
LINT_PYTHON := $(LINT_VENV)/bin/python
LINT_LOCK := requirements-lint.txt
LINT_PROJECT := tools/lint/pyproject.toml
RUFF := $(LINT_VENV)/bin/ruff
SYSTEM_PYTHON ?= python3
RUNTIME_LOCK := requirements.txt
DEVELOPMENT_LOCK := requirements-dev.txt
# --generate-hashes turns every install into a verified install. The header
# pip-compile writes is also what lets Dependabot recompile the whole tree
# instead of editing one line, so never add --no-header.
COMPILE := --quiet --strip-extras --allow-unsafe --generate-hashes
LOCK_UPGRADE ?=
LAUNCHER := ./remote-ssh-mcp
SOURCES := remote_ssh_mcp tests remote-ssh-mcp.py
COVERAGE_REPORT := coverage-report.md
COVERAGE_TOTAL := coverage-total.txt
PR_COMMENT := .github/scripts/pr-comment.sh
ANNOTATE := .github/scripts/annotate-diagnostics.sh
# Every Bash file in the repository, so syntax and lint cover all of them.
SHELL_FILES := $(LAUNCHER) tests/live-target.sh \
	containers/toolbox/entrypoint.sh containers/live-target/entrypoint.sh \
	$(PR_COMMENT) $(ANNOTATE)
COVERAGE_DATA := .coverage
# CI sets RUFF_OUTPUT_FORMAT=github to annotate the diff; local runs keep the
# default Ruff rendering.
RUFF_OUTPUT_FORMAT ?=
RUFF_OUTPUT := $(if $(RUFF_OUTPUT_FORMAT),--output-format=$(RUFF_OUTPUT_FORMAT))
# pyproject.toml owns the single coverage threshold; never duplicate the value.
READ_THRESHOLD := import pathlib, tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["tool"]["coverage"]["report"]["fail_under"])
COVERAGE_THRESHOLD = $(shell $(SYSTEM_PYTHON) -c '$(READ_THRESHOLD)')

include make/container.mk
include make/live.mk

.PHONY: help runtime-venv lint-venv lock refresh-dependencies freeze-check format lint \
	typecheck bandit audit test coverage-report syntax shellcheck check ci clean

help:
	@printf '%s\n' \
		'make runtime-venv  Install the explicit runtime-only host environment' \
		'make host-tests    Run host tests; requires a prepared runtime venv' \
		'make lock          Recompile all three locks after a dependency change' \
		'make refresh-dependencies' \
		'                   Recompile all locks at latest compatible versions' \
		'make format        Apply Ruff fixes and formatting' \
		'make lint          Check Ruff lint and formatting' \
		'make typecheck     Run strict mypy static type analysis' \
		'make bandit       Scan Python runtime code for security issues' \
		'make audit         Scan the installed dependency tree for CVEs' \
		'make test          Run pytest in parallel worker processes' \
		'make coverage-report' \
		'                   Print the Markdown coverage report for the last test run' \
		'make shellcheck    Check every repository Bash file with ShellCheck' \
		'make check         Run all local checks' \
		'make ci            Run local checks and the online dependency audit' \
		'make clean         Remove caches and generated build metadata' \
		'make live-preflight' \
		'                   Validate the automatic ephemeral-key live test' \
		'make live-test    Run live test; requires a prepared runtime venv' \
		'make live-fido-preflight PUBLIC_KEY=... IDENTITY_FILE=...' \
		'                   Validate the hardware-key live test' \
		'make live-fido-test PUBLIC_KEY=... IDENTITY_FILE=...' \
		'                   Run the hardware-key live test'

# This is an explicit installation step. The launcher never creates an
# environment or invokes pip when an MCP client starts it.
runtime-venv:
	@command -v $(SYSTEM_PYTHON) >/dev/null 2>&1 || { \
		printf 'required command not found: %s\n' '$(SYSTEM_PYTHON)' >&2; \
		exit 1; \
	}
	@$(SYSTEM_PYTHON) -c 'import sys; sys.exit("Python 3.11 or newer is required") if sys.version_info < (3, 11) else None'
	@if [[ ! -x "$(RUNTIME_PYTHON)" ]] || \
		[[ ! -f "$(RUNTIME_STATE)" ]] || \
		! cmp -s $(RUNTIME_LOCK) "$(RUNTIME_STATE)" || \
		"$(RUNTIME_PYTHON)" -c 'import pip' >/dev/null 2>&1; then \
		if [[ -e "$(RUNTIME_VENV)" ]]; then \
			find "$(RUNTIME_VENV)" -depth -delete; \
		fi; \
		$(SYSTEM_PYTHON) -m venv "$(RUNTIME_VENV)"; \
		$(RUNTIME_PYTHON) -m pip install --quiet --require-hashes \
			--only-binary=:all: --requirement $(RUNTIME_LOCK); \
		$(RUNTIME_PYTHON) -m pip uninstall --quiet --yes pip; \
		cp -- $(RUNTIME_LOCK) "$(RUNTIME_STATE)"; \
	fi

# Compiling locks resolves dependencies, so it needs the network, and it writes
# the result back to the work tree.
lock: lock-image
	@$(BOX_ARCHIVE) | $(PODMAN) run $(LOCK_ONLINE) \
		--env BOX_EXPORT="$(RUNTIME_LOCK) $(DEVELOPMENT_LOCK) $(LINT_LOCK)" \
		--env BOX_EXPORT_ON_SUCCESS=1 $(LOCK_TAG) \
		sh -ceu 'python -m piptools compile $(COMPILE) $(LOCK_UPGRADE) \
			--output-file=$(RUNTIME_LOCK) pyproject.toml; \
			python -m piptools compile $(COMPILE) $(LOCK_UPGRADE) --extra=dev \
				--output-file=$(DEVELOPMENT_LOCK) pyproject.toml; \
			python -m piptools compile $(COMPILE) $(LOCK_UPGRADE) \
				--output-file=$(LINT_LOCK) $(LINT_PROJECT); \
			chmod 0644 $(RUNTIME_LOCK) $(DEVELOPMENT_LOCK) $(LINT_LOCK)' \
		| tar --extract --file=- --no-same-owner

refresh-dependencies:
	@$(MAKE) lock LOCK_UPGRADE=--upgrade
	@for path in $(RUNTIME_VENV) $(LINT_VENV); do \
		if [[ -e "$$path" ]]; then find "$$path" -depth -delete; fi; \
	done
	@$(MAKE) lint-venv

# Recompiling without --upgrade keeps every satisfiable pin, so this reports
# only a lock that no longer matches pyproject.toml.
freeze-check: lock-image
	@$(BOX_ARCHIVE) | $(PODMAN) run $(LOCK_ONLINE) $(LOCK_TAG) \
		bash -ceu 'cp $(RUNTIME_LOCK) /tmp/runtime.txt; \
		cp $(DEVELOPMENT_LOCK) /tmp/development.txt; \
		cp $(LINT_LOCK) /tmp/lint.txt; \
		python -m piptools compile $(COMPILE) \
			--output-file=/tmp/runtime.txt pyproject.toml; \
		python -m piptools compile $(COMPILE) --extra=dev \
			--output-file=/tmp/development.txt pyproject.toml; \
		python -m piptools compile $(COMPILE) \
			--output-file=/tmp/lint.txt $(LINT_PROJECT); \
		diff -u <(sed "/^[[:space:]]*#/d" $(RUNTIME_LOCK)) \
			<(sed "/^[[:space:]]*#/d" /tmp/runtime.txt); \
		diff -u <(sed "/^[[:space:]]*#/d" $(DEVELOPMENT_LOCK)) \
			<(sed "/^[[:space:]]*#/d" /tmp/development.txt); \
		diff -u <(sed "/^[[:space:]]*#/d" $(LINT_LOCK)) \
			<(sed "/^[[:space:]]*#/d" /tmp/lint.txt)'

# Formatting and lint run on the host so an editor shares one Ruff version
# with CI. Every other check runs in the toolbox container.

# Recreate the environment whenever the lock changes so a package removed from
# the lock cannot remain installed on the host.
lint-venv:
	@command -v $(SYSTEM_PYTHON) >/dev/null 2>&1 || { \
		printf 'required command not found: %s\n' '$(SYSTEM_PYTHON)' >&2; \
		exit 1; \
	}
	@if [[ ! -x "$(RUFF)" ]] || \
		[[ ! -f "$(LINT_VENV)/.$(LINT_LOCK)" ]] || \
		! cmp -s $(LINT_LOCK) "$(LINT_VENV)/.$(LINT_LOCK)"; then \
		if [[ -e "$(LINT_VENV)" ]]; then find "$(LINT_VENV)" -depth -delete; fi; \
		$(SYSTEM_PYTHON) -m venv "$(LINT_VENV)"; \
		$(LINT_PYTHON) -m pip install --quiet --require-hashes \
			--only-binary=:all: --requirement $(LINT_LOCK); \
		cp -- $(LINT_LOCK) "$(LINT_VENV)/.$(LINT_LOCK)"; \
	fi

format: lint-venv
	@$(RUFF) check --fix $(SOURCES)
	@$(RUFF) format $(SOURCES)

lint: lint-venv
	@$(RUFF) check $(RUFF_OUTPUT) $(SOURCES)
	@$(RUFF) format --check $(SOURCES)

# mypy and Bandit read the code without running it, but mypy needs the runtime
# dependency tree to resolve types, so they stay in the container where that
# tree already lives.
typecheck: toolbox-image
	@$(BOX_ARCHIVE) | $(PODMAN) run $(BOX_CONFINE) $(TOOLBOX_TAG) \
		python -m mypy | $(ANNOTATE) mypy

bandit: toolbox-image
	$(BOX_RUN) python -m bandit -q -c pyproject.toml -r \
		remote_ssh_mcp remote-ssh-mcp.py

# The audit resolves advisories, which is the one reason a check may reach the
# network. It inspects the toolbox's own installed tree, which is the
# development lock.
audit: toolbox-image
	$(BOX_RUN_ONLINE) python -m pip_audit --local --strict

# The coverage report is produced beside the run that measured it: the data
# file never leaves the container, so regenerating it later would be a lie.
test: toolbox-image
	@mkdir -p $(ARTIFACTS)
	@$(BOX_ARCHIVE) | $(PODMAN) run $(BOX_CONFINE) \
		--env BOX_EXPORT="coverage.xml $(COVERAGE_REPORT) $(COVERAGE_TOTAL)" \
		$(TOOLBOX_TAG) sh -ceu 'status=0; python -m pytest || status=$$?; \
			python -m coverage report --format=markdown > $(COVERAGE_REPORT); \
			python -m coverage report --format=total > $(COVERAGE_TOTAL); \
			exit $$status' \
		| tar --extract --file=- --directory=$(ARTIFACTS) --no-same-owner

# Composed here rather than in the container, so the report stays a plain table
# on the inside and the framing lives with the threshold that defines it.
coverage-report:
	@if [[ ! -f $(ARTIFACTS)/$(COVERAGE_TOTAL) ]]; then \
		printf '%s\n' 'No coverage data; run make test first.' >&2; \
		exit 1; \
	fi
	@total=$$(cat -- $(ARTIFACTS)/$(COVERAGE_TOTAL)); \
		threshold='$(COVERAGE_THRESHOLD)'; \
		verdict=$$(awk -v total="$$total" -v threshold="$$threshold" \
			'BEGIN { print (total + 0 >= threshold + 0) ? "above" : "below" }'); \
		printf '### Coverage: %s%% (threshold %s%%, %s)\n\n' \
			"$$total" "$$threshold" "$$verdict"; \
		printf '%s\n\n' \
			'<details><summary>Per-file coverage, fully covered files hidden</summary>'; \
		cat -- $(ARTIFACTS)/$(COVERAGE_REPORT); \
		printf '\n%s\n' '</details>'

syntax: toolbox-image
	$(BOX_RUN) sh -ceu 'python -m compileall -q remote_ssh_mcp \
		remote-ssh-mcp.py tests/live_podman_e2e.py; bash -n $(SHELL_FILES)'

# The toolbox carries ShellCheck, so this can no longer be skipped silently.
shellcheck: toolbox-image
	$(BOX_RUN) shellcheck $(SHELL_FILES)

check: lint typecheck bandit test syntax shellcheck freeze-check

ci: check audit

clean:
	@find remote_ssh_mcp tests -type f -path '*/__pycache__/*' -delete
	@find remote_ssh_mcp tests -depth -type d -name __pycache__ -empty -delete
	@for path in .pytest_cache .ruff_cache __pycache__ build dist *.egg-info; do \
		if [[ -d "$$path" ]]; then find "$$path" -depth -delete; fi; \
	done
	@for path in $(COVERAGE_DATA) coverage.xml coverage-report.md \
		$(ARTIFACTS)/coverage.xml $(ARTIFACTS)/$(COVERAGE_REPORT) \
		$(ARTIFACTS)/$(COVERAGE_TOTAL) $(ARTIFACTS)/pr-coverage.md; do \
		if [[ -e "$$path" ]]; then unlink "$$path"; fi; \
	done
