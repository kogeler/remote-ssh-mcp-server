# MCP live SSH test targets.
#
# The automatic workflow keeps the matrix driver on the host but runs both the
# application companion and SSH target in containers on a private network.
# FIDO keeps the application on the host because OpenSSH must reach the
# operator's hardware key.

LIVE_HARNESS := tests/live_harness.py
PUBLIC_KEY ?=
IDENTITY_FILE ?=
.PHONY: live-preflight live-test live-fido-preflight live-fido-test \
	live-fido-sanitized-preflight live-fido-sanitized-test

live-preflight:
	@PODMAN='$(PODMAN)' $(RUNTIME_PYTHON) $(LIVE_HARNESS) \
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
	@PODMAN='$(PODMAN)' $(RUNTIME_PYTHON) $(LIVE_HARNESS) \
		--mode fido --image $(TARGET_TAG) --preflight-only \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)"

live-fido-test: live-target-image host-tests
	@PODMAN='$(PODMAN)' \
		REMOTE_SSH_MCP_LIVE_TARGET_CONFINE='$(LIVE_TARGET_CONFINE)' \
		$(RUNTIME_PYTHON) $(LIVE_HARNESS) --mode fido --image $(TARGET_TAG) \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)"

live-fido-sanitized-preflight:
	@PODMAN='$(PODMAN)' $(RUNTIME_PYTHON) $(LIVE_HARNESS) \
		--mode fido --image $(TARGET_TAG) --preflight-only \
		--strip-session-environment \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)"

live-fido-sanitized-test: live-target-image host-tests
	@PODMAN='$(PODMAN)' \
		REMOTE_SSH_MCP_LIVE_TARGET_CONFINE='$(LIVE_TARGET_CONFINE)' \
		$(RUNTIME_PYTHON) $(LIVE_HARNESS) --mode fido --image $(TARGET_TAG) \
		--strip-session-environment \
		--public-key "$(PUBLIC_KEY)" \
		--identity-file "$(IDENTITY_FILE)"
