# Live MCP test targets.
#
# The automatic workflow keeps the matrix driver on the host but runs both the
# MCP server and SSH target in containers on a private network. FIDO keeps the
# server on the host because OpenSSH must reach the operator's hardware key.

LIVE_HARNESS := tests/live_harness.py
PUBLIC_KEY ?=
IDENTITY_FILE ?=

.PHONY: host-tests live-preflight live-test live-fido-preflight live-fido-test

# Tests that need the host itself: Podman on the machine, or real launcher
# subprocesses. Their development environment exists only for this recipe and
# is removed on success, failure, or interruption. The runtime venv is a
# separate, explicit prerequisite and is never installed by a test target.
host-tests:
	@host_venv=$$(mktemp -d "$${TMPDIR:-/tmp}/remote-ssh-mcp-host-tests.XXXXXXXX"); \
		cleanup() { find "$$host_venv" -depth -delete; }; \
		trap cleanup EXIT; \
		trap 'exit 129' HUP; \
		trap 'exit 130' INT; \
		trap 'exit 143' TERM; \
		$(SYSTEM_PYTHON) -m venv "$$host_venv"; \
		"$$host_venv/bin/python" -m pip install --quiet --require-hashes \
			--only-binary=:all: --requirement $(DEVELOPMENT_LOCK); \
		"$$host_venv/bin/python" -m pytest -m host --no-cov

live-preflight:
	@PODMAN='$(PODMAN)' $(SYSTEM_PYTHON) $(LIVE_HARNESS) \
		--mode ephemeral --image $(TARGET_TAG) \
		--server-image $(TOOLBOX_TAG) --preflight-only

live-test: toolbox-image live-target-image
	@$(BOX_ARCHIVE) | \
		PODMAN='$(PODMAN)' \
		REMOTE_SSH_MCP_LIVE_SERVER_CONFINE='$(LIVE_SERVER_CONFINE)' \
		REMOTE_SSH_MCP_LIVE_TARGET_CONFINE='$(LIVE_TARGET_CONFINE)' \
		$(RUNTIME_PYTHON) $(LIVE_HARNESS) \
		--mode ephemeral --image $(TARGET_TAG) \
		--server-image $(TOOLBOX_TAG)

live-fido-preflight:
	@PODMAN='$(PODMAN)' $(SYSTEM_PYTHON) $(LIVE_HARNESS) \
		--mode fido --image $(TARGET_TAG) --preflight-only \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)"

live-fido-test: live-target-image host-tests
	@PODMAN='$(PODMAN)' \
		REMOTE_SSH_MCP_LIVE_TARGET_CONFINE='$(LIVE_TARGET_CONFINE)' \
		$(RUNTIME_PYTHON) $(LIVE_HARNESS) --mode fido --image $(TARGET_TAG) \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)"
