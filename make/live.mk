# Live MCP test targets.
#
# The live test is the one case that deliberately runs on the host: it proves
# that the launcher, a real OpenSSH client, and a real SSH master behave the way
# an operator's machine behaves. Only the target it connects to is a container.

LIVE_HARNESS := tests/live-target.sh
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
	@$(LIVE_HARNESS) --mode ephemeral --image $(TARGET_TAG) --preflight-only

live-test: live-target-image
	@$(LIVE_HARNESS) --mode ephemeral --image $(TARGET_TAG)

live-fido-preflight:
	@$(LIVE_HARNESS) --mode fido --image $(TARGET_TAG) --preflight-only \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)"

live-fido-test: live-target-image host-tests
	@$(LIVE_HARNESS) --mode fido --image $(TARGET_TAG) \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)"
