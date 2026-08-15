SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

PYTHON := venv/bin/python
RUFF := venv/bin/ruff
MYPY := venv/bin/mypy
BANDIT := venv/bin/bandit
SYSTEM_PYTHON ?= python3
VENV_DIR := venv
PIP_COMPILE := venv/bin/pip-compile
RUNTIME_LOCK := requirements.txt
DEVELOPMENT_LOCK := requirements-dev.txt
# --generate-hashes turns every install into a verified install. The header
# pip-compile writes is also what lets Dependabot recompile the whole tree
# instead of editing one line, so never add --no-header.
COMPILE := --quiet --strip-extras --allow-unsafe --generate-hashes
LAUNCHER := ./remote-ssh-mcp
LIVE_HARNESS := tests/run-live-lxc.sh
LIVE_FIDO_HARNESS := tests/run-live-fido-lxc.sh
LIVE_CORE := tests/run-live-lxc-core.sh
PR_COMMENT := .github/scripts/pr-comment.sh
ANNOTATE := .github/scripts/annotate-diagnostics.sh
COVERAGE_DATA := .coverage
# CI sets RUFF_OUTPUT_FORMAT=github to annotate the diff; local runs keep the
# default Ruff rendering.
RUFF_OUTPUT_FORMAT ?=
RUFF_OUTPUT := $(if $(RUFF_OUTPUT_FORMAT),--output-format=$(RUFF_OUTPUT_FORMAT))
# pyproject.toml owns the single coverage threshold; never duplicate the value.
READ_THRESHOLD := import pathlib, tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["tool"]["coverage"]["report"]["fail_under"])
COVERAGE_THRESHOLD = $(shell $(PYTHON) -c '$(READ_THRESHOLD)')
PUBLIC_KEY ?=
IDENTITY_FILE ?=
LXC_IMAGE ?= images:debian/13

.PHONY: help venv lock refresh-dependencies freeze-check format lint \
	typecheck bandit audit test coverage-report syntax shellcheck check ci clean \
	live-preflight live-test \
	live-fido-preflight live-fido-test

help:
	@printf '%s\n' \
		'make venv          Bootstrap or update the tool-local virtual environment' \
		'make lock          Recompile both locks after a pyproject.toml change' \
		'make refresh-dependencies' \
		'                   Rebuild venv and recompile both locks at latest versions' \
		'make format        Apply Ruff fixes and formatting' \
		'make lint          Check Ruff lint and formatting' \
		'make typecheck     Run strict mypy static type analysis' \
		'make bandit       Scan Python runtime code for security issues' \
		'make audit         Scan the installed dependency tree for CVEs' \
		'make test          Run pytest in parallel worker processes' \
		'make coverage-report' \
		'                   Print the Markdown coverage report for the last test run' \
		'make shellcheck    Check Bash files when ShellCheck is installed' \
		'make check         Run all local checks' \
		'make ci            Run local checks and the online dependency audit' \
		'make clean         Remove caches and generated build metadata' \
		'make live-preflight [LXC_IMAGE=...]' \
		'                   Validate the automatic ephemeral-key LXC test' \
		'make live-test [LXC_IMAGE=...]' \
		'                   Run the automatic ephemeral-key LXC test' \
		'make live-fido-preflight PUBLIC_KEY=... IDENTITY_FILE=...' \
		'                   Validate the hardware-key LXC test' \
		'make live-fido-test PUBLIC_KEY=... IDENTITY_FILE=... [LXC_IMAGE=...]' \
		'                   Run the hardware-key LXC test'

# The launcher installs the runtime lock only. Development tools live in the
# same environment but come from the development lock, so an installed server
# never carries a linter, a type checker, or a test runner.
venv:
	@$(LAUNCHER) --help >/dev/null
	@if [[ ! -f "$(VENV_DIR)/.$(DEVELOPMENT_LOCK)" ]] || \
		! cmp -s $(DEVELOPMENT_LOCK) "$(VENV_DIR)/.$(DEVELOPMENT_LOCK)"; then \
		$(PYTHON) -m pip install --requirement $(DEVELOPMENT_LOCK); \
		cp -- $(DEVELOPMENT_LOCK) "$(VENV_DIR)/.$(DEVELOPMENT_LOCK)"; \
	fi

lock: venv
	@$(PIP_COMPILE) $(COMPILE) --output-file=$(RUNTIME_LOCK) pyproject.toml
	@$(PIP_COMPILE) $(COMPILE) --extra=dev \
		--output-file=$(DEVELOPMENT_LOCK) pyproject.toml
	@for path in build dist *.egg-info; do \
		if [[ -d "$$path" ]]; then find "$$path" -depth -delete; fi; \
	done

refresh-dependencies:
	@command -v $(SYSTEM_PYTHON) >/dev/null 2>&1 || { \
		printf 'required command not found: %s\n' '$(SYSTEM_PYTHON)' >&2; \
		exit 1; \
	}
	@if [[ -e "$(VENV_DIR)" ]]; then find "$(VENV_DIR)" -depth -delete; fi
	@$(SYSTEM_PYTHON) -m venv "$(VENV_DIR)"
	@$(PYTHON) -m pip install --upgrade pip
	@$(PYTHON) -m pip install --upgrade pip-tools
	@$(PIP_COMPILE) $(COMPILE) --upgrade \
		--output-file=$(RUNTIME_LOCK) pyproject.toml
	@$(PIP_COMPILE) $(COMPILE) --upgrade --extra=dev \
		--output-file=$(DEVELOPMENT_LOCK) pyproject.toml
	@$(PYTHON) -m pip install --requirement $(DEVELOPMENT_LOCK)
	@cp -- $(RUNTIME_LOCK) "$(VENV_DIR)/.$(RUNTIME_LOCK)"
	@cp -- $(DEVELOPMENT_LOCK) "$(VENV_DIR)/.$(DEVELOPMENT_LOCK)"
	@for path in build dist *.egg-info; do \
		if [[ -d "$$path" ]]; then find "$$path" -depth -delete; fi; \
	done

# Recompiling without --upgrade keeps every satisfiable pin, so this reports
# only a lock that no longer matches pyproject.toml.
freeze-check: venv
	@runtime=$$(mktemp $(RUNTIME_LOCK).XXXXXXXX); \
		development=$$(mktemp $(DEVELOPMENT_LOCK).XXXXXXXX); \
		trap 'rm -f -- "$$runtime" "$$development"' EXIT; \
		cp -- $(RUNTIME_LOCK) "$$runtime"; \
		cp -- $(DEVELOPMENT_LOCK) "$$development"; \
		$(PIP_COMPILE) $(COMPILE) --output-file="$$runtime" pyproject.toml; \
		$(PIP_COMPILE) $(COMPILE) --extra=dev \
			--output-file="$$development" pyproject.toml; \
		diff -u <(grep -v '^#' $(RUNTIME_LOCK)) <(grep -v '^#' "$$runtime"); \
		diff -u <(grep -v '^#' $(DEVELOPMENT_LOCK)) \
			<(grep -v '^#' "$$development")

format: venv
	@$(RUFF) check --fix remote_ssh_mcp tests remote-ssh-mcp.py
	@$(RUFF) format remote_ssh_mcp tests remote-ssh-mcp.py

lint: venv
	@$(RUFF) check $(RUFF_OUTPUT) remote_ssh_mcp tests remote-ssh-mcp.py
	@$(RUFF) format --check remote_ssh_mcp tests remote-ssh-mcp.py

typecheck: venv
	@$(MYPY) | $(ANNOTATE) mypy

bandit: venv
	@$(BANDIT) -q -c pyproject.toml -r remote_ssh_mcp remote-ssh-mcp.py

audit: venv
	@$(PYTHON) -m pip_audit --local --strict

test: venv
	@$(PYTHON) -m pytest

coverage-report: venv
	@if [[ ! -f $(COVERAGE_DATA) ]]; then \
		printf '%s\n' 'No coverage data; run make test first.' >&2; \
		exit 1; \
	fi
	@total=$$($(PYTHON) -m coverage report --format=total); \
		threshold='$(COVERAGE_THRESHOLD)'; \
		verdict=$$(awk -v total="$$total" -v threshold="$$threshold" \
			'BEGIN { print (total + 0 >= threshold + 0) ? "above" : "below" }'); \
		printf '### Coverage: %s%% (threshold %s%%, %s)\n\n' \
			"$$total" "$$threshold" "$$verdict"; \
		printf '%s\n\n' \
			'<details><summary>Per-file coverage, fully covered files hidden</summary>'; \
		$(PYTHON) -m coverage report --format=markdown; \
		printf '\n%s\n' '</details>'

syntax: venv
	@$(PYTHON) -m compileall -q remote_ssh_mcp remote-ssh-mcp.py tests/live_lxc_e2e.py
	@bash -n $(LAUNCHER) $(LIVE_HARNESS) $(LIVE_FIDO_HARNESS) $(LIVE_CORE) $(PR_COMMENT) $(ANNOTATE)

shellcheck:
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck $(LAUNCHER) $(LIVE_HARNESS) $(LIVE_FIDO_HARNESS) $(LIVE_CORE) \
			$(PR_COMMENT) $(ANNOTATE); \
	else \
		printf '%s\n' 'shellcheck is not installed; skipping'; \
	fi

check: lint typecheck bandit test syntax shellcheck freeze-check

ci: check audit

clean:
	@find remote_ssh_mcp tests -type f -path '*/__pycache__/*' -delete
	@find remote_ssh_mcp tests -depth -type d -name __pycache__ -empty -delete
	@for path in .pytest_cache .ruff_cache __pycache__ build dist *.egg-info; do \
		if [[ -d "$$path" ]]; then find "$$path" -depth -delete; fi; \
	done
	@for path in $(COVERAGE_DATA) coverage.xml coverage-report.md; do \
		if [[ -e "$$path" ]]; then unlink "$$path"; fi; \
	done

live-preflight: venv
	@$(LIVE_HARNESS) --preflight-only --image "$(LXC_IMAGE)"

live-test: venv
	@$(LIVE_HARNESS) --image "$(LXC_IMAGE)"

live-fido-preflight: venv
	@$(LIVE_FIDO_HARNESS) --preflight-only \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)" \
		--image "$(LXC_IMAGE)"

live-fido-test: venv
	@$(LIVE_FIDO_HARNESS) \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)" \
		--image "$(LXC_IMAGE)"
