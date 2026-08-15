SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

PYTHON := venv/bin/python
RUFF := venv/bin/ruff
MYPY := venv/bin/mypy
BANDIT := venv/bin/bandit
SYSTEM_PYTHON ?= python3
VENV_DIR := venv
PROJECT_PACKAGE := remote-ssh-mcp
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

.PHONY: help venv freeze refresh-dependencies freeze-check format lint \
	typecheck bandit audit test coverage-report syntax shellcheck check ci clean \
	live-preflight live-test \
	live-fido-preflight live-fido-test

help:
	@printf '%s\n' \
		'make venv          Bootstrap or update the tool-local virtual environment' \
		'make freeze        Replace requirements.txt with the complete pip freeze' \
		'make refresh-dependencies' \
		'                   Rebuild venv from pyproject.toml and freeze the full tree' \
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

venv:
	@$(LAUNCHER) --help >/dev/null

freeze: venv
	@temporary=$$(mktemp requirements.txt.XXXXXXXX); \
		trap '[[ ! -e "$$temporary" ]] || unlink "$$temporary"' EXIT; \
		$(PYTHON) -m pip freeze --exclude $(PROJECT_PACKAGE) > "$$temporary"; \
		chmod 0644 "$$temporary"; \
		mv -- "$$temporary" requirements.txt; \
		trap - EXIT
	@cp -- requirements.txt venv/.requirements.txt
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
	@$(PYTHON) -m pip install --upgrade '.[dev]'
	@$(PYTHON) -m pip uninstall --yes $(PROJECT_PACKAGE)
	@temporary=$$(mktemp requirements.txt.XXXXXXXX); \
		trap '[[ ! -e "$$temporary" ]] || unlink "$$temporary"' EXIT; \
		$(PYTHON) -m pip freeze --exclude $(PROJECT_PACKAGE) > "$$temporary"; \
		chmod 0644 "$$temporary"; \
		mv -- "$$temporary" requirements.txt; \
		trap - EXIT
	@cp -- requirements.txt venv/.requirements.txt
	@for path in build dist *.egg-info; do \
		if [[ -d "$$path" ]]; then find "$$path" -depth -delete; fi; \
	done

freeze-check: venv
	@diff -u requirements.txt <($(PYTHON) -m pip freeze --exclude $(PROJECT_PACKAGE))

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
