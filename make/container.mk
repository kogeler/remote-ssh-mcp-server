# Container policy: which image, which confinement, when to rebuild.
#
# Checks that need the project environment run inside the toolbox image. Ruff is
# the deliberate exception and uses its own host venv for editor integration.
# The work tree is streamed in as a tar and is never bind-mounted, so a
# container check cannot reach the host filesystem, virtual environments, or
# Git history.
#
# The imperative part of a container run lives in the image entry point
# (containers/toolbox/entrypoint.sh), which is why nothing here needs a wrapper
# script on the host.

PODMAN ?= podman
ARTIFACTS := .artifacts

# The tag is derived from everything that goes into the build, so an unchanged
# context always resolves to an image that is already present, and a changed one
# can never be served from a stale cache. Lazy assignment keeps the hash off the
# critical path of targets that do not need an image.
TOOLBOX_CONTEXT := containers/toolbox/Containerfile containers/toolbox/entrypoint.sh
TOOLBOX_KEY = $(shell cat $(TOOLBOX_CONTEXT) requirements-dev.txt | \
	sha256sum | cut -c1-16)
TOOLBOX_TAG = localhost/remote-ssh-mcp-toolbox:$(TOOLBOX_KEY)

LOCK_CONTEXT := containers/toolbox/Containerfile containers/toolbox/entrypoint.sh
LOCK_KEY = $(shell cat $(LOCK_CONTEXT) | sha256sum | cut -c1-16)
LOCK_TAG = localhost/remote-ssh-mcp-lock:$(LOCK_KEY)

TARGET_CONTEXT := containers/live-target/Containerfile \
	containers/live-target/entrypoint.sh containers/live-target/sshd.conf
TARGET_KEY = $(shell cat $(TARGET_CONTEXT) | sha256sum | cut -c1-16)
TARGET_TAG = localhost/remote-ssh-mcp-live-target:$(TARGET_KEY)

# Confinement applied to every toolbox container.
#
# --userns=auto maps the container to a subordinate id range instead of to the
# invoking user, so an escape lands on an account that owns nothing on the host.
# The range is deliberately small so several containers fit in one allocation.
BOX_CONFINE = \
	--rm \
	--interactive \
	--network=none \
	--userns=auto:size=2048 \
	--security-opt=no-new-privileges \
	--cap-drop=ALL \
	--read-only \
	--read-only-tmpfs=false \
	--ipc=private \
	--pid=private \
	--uts=private \
	--cgroupns=private \
	--systemd=false \
	--no-hosts \
	--unsetenv-all \
	--umask=077 \
	--pids-limit=1024 \
	--memory=2g \
	--memory-swap=2g \
	--ulimit=nofile=4096:4096 \
	--log-driver=none \
	--timeout=1800 \
	--pull=never \
	--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777 \
	--tmpfs=/work:rw,exec,nosuid,nodev,size=2g,mode=1777 \
	--env HOME=/tmp/home \
	--env LANG=C.UTF-8 \
	--env LC_ALL=C.UTF-8 \
	--env TZ=UTC \
	--env PATH=/usr/local/bin:/usr/bin:/bin

# Only the targets that resolve dependencies may reach the network.
BOX_ONLINE = $(subst --network=none,--network=slirp4netns,$(BOX_CONFINE))

# A full --upgrade hash refresh downloads artifacts for every supported wheel.
# Keep that burst isolated and bounded without constraining ordinary checks to
# the resolver's larger temporary workspace.
LOCK_ONLINE = $(subst --memory=2g,--memory=4g,\
	$(subst --memory-swap=2g,--memory-swap=4g,\
	$(subst size=512m,size=4g,$(BOX_ONLINE))))

# Exactly the current project inputs: tracked files plus ordinary untracked
# files the checkout does not exclude. Local edits survive; build output,
# caches, the virtual environment, and .git never enter a container.
BOX_ARCHIVE = git ls-files --cached --others --exclude-standard -z \
	| sort -z \
	| tar --create --file=- --null --verbatim-files-from --files-from=-

BOX_RUN = @$(BOX_ARCHIVE) | $(PODMAN) run $(BOX_CONFINE) $(TOOLBOX_TAG)
BOX_RUN_ONLINE = @$(BOX_ARCHIVE) | $(PODMAN) run $(BOX_ONLINE) $(TOOLBOX_TAG)

# Collect named paths from the work tree after the command finishes. The command
# writes its own output to stderr in this mode, so stdout carries only the tar.
BOX_COLLECT = @mkdir -p $(ARTIFACTS); $(BOX_ARCHIVE) | \
	$(PODMAN) run $(BOX_CONFINE) --env BOX_EXPORT="$(BOX_EXPORT)" $(TOOLBOX_TAG)

.PHONY: images toolbox-image lock-image live-target-image image-key image-save image-load \
	doctor clean-containers

images: toolbox-image live-target-image

# A content-addressed tag that already exists is by definition current, so the
# check is the whole rebuild rule. An image tag cannot be a Make target: it
# contains a colon.
toolbox-image:
	@$(PODMAN) image exists $(TOOLBOX_TAG) || { \
		printf 'building %s\n' '$(TOOLBOX_TAG)' >&2; \
		$(PODMAN) build --quiet --pull=missing --tag $(TOOLBOX_TAG) \
			--target dev \
			--file containers/toolbox/Containerfile . >/dev/null; \
	}

lock-image:
	@$(PODMAN) image exists $(LOCK_TAG) || { \
		printf 'building %s\n' '$(LOCK_TAG)' >&2; \
		$(PODMAN) build --quiet --pull=missing --tag $(LOCK_TAG) \
			--target lock \
			--file containers/toolbox/Containerfile . >/dev/null; \
	}

live-target-image:
	@$(PODMAN) image exists $(TARGET_TAG) || { \
		printf 'building %s\n' '$(TARGET_TAG)' >&2; \
		$(PODMAN) build --quiet --pull=missing --tag $(TARGET_TAG) \
			--file containers/live-target/Containerfile \
			containers/live-target >/dev/null; \
	}

# CI keys its image cache on these, so a cache entry can never outlive the
# context that produced it.
# Emitted as key=value lines so a workflow can consume them directly.
image-key:
	@printf 'toolbox=%s\ntarget=%s\n' '$(TOOLBOX_KEY)' '$(TARGET_KEY)'

image-save: images
	@mkdir -p $(ARTIFACTS)
	@$(PODMAN) save --quiet --format oci-archive \
		--output $(ARTIFACTS)/toolbox.tar $(TOOLBOX_TAG)
	@$(PODMAN) save --quiet --format oci-archive \
		--output $(ARTIFACTS)/live-target.tar $(TARGET_TAG)

image-load:
	@for archive in $(ARTIFACTS)/toolbox.tar $(ARTIFACTS)/live-target.tar; do \
		if [[ -r "$$archive" ]]; then \
			$(PODMAN) load --quiet --input "$$archive" >/dev/null; \
		fi; \
	done

doctor:
	@$(PODMAN) info >/dev/null 2>&1 || { \
		printf 'podman is unavailable\n' >&2; exit 1; \
	}
	@rootless=$$($(PODMAN) info --format '{{.Host.Security.Rootless}}'); \
		if [[ "$$rootless" != true ]]; then \
			printf 'rootless podman is required, got %s\n' "$$rootless" >&2; \
			exit 1; \
		fi
	@printf '%s\n' \
		"podman   $$($(PODMAN) version --format '{{.Client.Version}}')" \
		'rootless yes' \
		'toolbox  $(TOOLBOX_TAG)' \
		'resolver $(LOCK_TAG)' \
		'target   $(TARGET_TAG)'

clean-containers:
	@stale=$$($(PODMAN) ps --all --quiet \
		--filter 'label=remote-ssh-mcp.owner' 2>/dev/null); \
		if [[ -n "$$stale" ]]; then \
			$(PODMAN) rm --force --time 5 $$stale >/dev/null; \
		fi
	@networks=$$($(PODMAN) network ls --quiet \
		--filter 'label=remote-ssh-mcp.owner' 2>/dev/null); \
		if [[ -n "$$networks" ]]; then \
			$(PODMAN) network rm --force $$networks >/dev/null; \
		fi
	@images=$$($(PODMAN) images --quiet \
		--filter 'reference=localhost/remote-ssh-mcp-*' 2>/dev/null); \
		if [[ -n "$$images" ]]; then \
			$(PODMAN) rmi --force $$images >/dev/null; \
		fi
