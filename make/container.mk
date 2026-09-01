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
IMAGE_ARTIFACTS := $(ARTIFACTS)/images

# Stable image tags are derived from everything that goes into the build, so an
# unchanged context resolves to an image already present and a changed one
# cannot be served from a stale cache.
TOOLBOX_CONTEXT := containers/toolbox/Containerfile \
	containers/toolbox/entrypoint.sh tools/container_payload.py
TOOLBOX_KEY = $(shell cat $(TOOLBOX_CONTEXT) requirements-dev.txt \
	| sha256sum | cut -c1-16)
TOOLBOX_TAG = localhost/remote-ssh-mcp-toolbox:$(TOOLBOX_KEY)

LOCK_CONTEXT := containers/toolbox/Containerfile \
	containers/toolbox/entrypoint.sh tools/container_payload.py
LOCK_KEY = $(shell cat $(LOCK_CONTEXT) | sha256sum | cut -c1-16)
LOCK_TAG = localhost/remote-ssh-mcp-lock:$(LOCK_KEY)

TARGET_CONTEXT := containers/live-target/Containerfile \
	containers/live-target/install-base.sh \
	containers/live-target/entrypoint.sh containers/live-target/sshd.conf
TARGET_KEY = $(shell cat $(TARGET_CONTEXT) | sha256sum | cut -c1-16)
TARGET_TAG = localhost/remote-ssh-mcp-live-target:$(TARGET_KEY)

COMPATIBILITY_CONTEXT := containers/compatibility/Containerfile \
	containers/toolbox/entrypoint.sh tools/container_payload.py
COMPATIBILITY_KEY = $(shell cat $(COMPATIBILITY_CONTEXT) | sha256sum | cut -c1-16)
COMPATIBILITY_TAG = localhost/remote-ssh-mcp-python313:$(COMPATIBILITY_KEY)

ACTIONLINT_CONTEXT := containers/actionlint/Containerfile \
	containers/toolbox/entrypoint.sh tools/container_payload.py
ACTIONLINT_KEY = $(shell { cat $(ACTIONLINT_CONTEXT); \
	printf '%s' '$(ACTIONLINT_IMAGE)'; } | sha256sum | cut -c1-16)
ACTIONLINT_TAG = localhost/remote-ssh-mcp-actionlint:$(ACTIONLINT_KEY)

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
	--memory=8g \
	--memory-swap=8g \
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

# The automatic live server uses the same user namespace, privilege, filesystem,
# resource, and environment policy as the toolbox. Its lifecycle and internal
# network are managed by the live harness, while a private home tmpfs holds the
# generated OpenSSH configuration on the otherwise read-only root filesystem.
LIVE_SERVER_CONFINE = \
	$(subst --env HOME=/tmp/home,--env HOME=/home/box,\
		$(filter-out --rm --interactive --network=none,$(BOX_CONFINE))) \
	--mount=type=tmpfs,destination=/home/box,tmpfs-size=16777216,tmpfs-mode=0700,chown=true

# The SSH target needs root inside rootless Podman's user namespace, setuid
# sudo, and writable system paths. These are the measured minimum privileges
# for sshd plus the fixture and rate-limit operations exercised by the matrix.
LIVE_TARGET_CONFINE = \
	--pull=never \
	--userns=auto:size=2048 \
	--cap-drop=ALL \
	--cap-add=AUDIT_WRITE \
	--cap-add=CHOWN \
	--cap-add=DAC_OVERRIDE \
	--cap-add=FOWNER \
	--cap-add=KILL \
	--cap-add=NET_ADMIN \
	--cap-add=NET_BIND_SERVICE \
	--cap-add=SETGID \
	--cap-add=SETUID \
	--cap-add=SYS_CHROOT \
	--ipc=private \
	--pid=private \
	--uts=private \
	--cgroupns=private \
	--systemd=false \
	--pids-limit=512 \
	--memory=1g \
	--memory-swap=1g \
	--log-driver=k8s-file \
	--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777

# A full --upgrade hash refresh downloads artifacts for every supported wheel.
# Keep that burst isolated and bounded without constraining ordinary checks to
# the resolver's larger temporary workspace.
LOCK_ONLINE = $(subst size=512m,size=4g,$(BOX_ONLINE))
COMPATIBILITY_ONLINE = $(subst size=512m,size=4g,$(BOX_ONLINE))

# Exactly this repository. The deterministic `remote_ssh_mcp/` archive prefix
# gives container entry points one fixed extraction root; caches, virtual
# environments, local state, and Git metadata can never enter the payload.
BOX_ARCHIVE = git ls-files --cached --others --exclude-standard -z -- . \
	| while IFS= read -r -d '' path; do \
		if [[ -e "$$path" || -L "$$path" ]]; then \
			printf 'remote_ssh_mcp/%s\0' "$$path"; \
		fi; \
	done \
	| sort -z \
	| $(SYSTEM_PYTHON) tools/container_payload.py create \
		--map-root=remote_ssh_mcp=. \
		--null-mapped-files-from-stdin

PAYLOAD_MERGE = $(SYSTEM_PYTHON) tools/container_payload.py merge \
	--destination=. --allow-empty

BOX_RUN = @$(BOX_ARCHIVE) | $(PODMAN) run $(BOX_CONFINE) $(TOOLBOX_TAG)
BOX_RUN_ONLINE = @$(BOX_ARCHIVE) | $(PODMAN) run $(BOX_ONLINE) $(TOOLBOX_TAG)

# Collect named paths from the work tree after the command finishes. The command
# writes its own output to stderr in this mode, so stdout carries only the tar.
BOX_COLLECT = @mkdir -p $(ARTIFACTS); $(BOX_ARCHIVE) | \
	$(PODMAN) run $(BOX_CONFINE) --env BOX_EXPORT="$(BOX_EXPORT)" $(TOOLBOX_TAG)

.PHONY: images toolbox-image lock-image live-target-image compatibility-image \
	image-key image-save image-save-toolbox image-save-resolver image-save-target \
	image-save-compatibility \
	image-load image-load-toolbox image-load-resolver image-load-target \
	image-load-compatibility \
	doctor clean-containers

images: toolbox-image live-target-image compatibility-image

# A content-addressed tag that already exists is by definition current, so the
# check is the whole rebuild rule. An image tag cannot be a Make target: it
# contains a colon.
toolbox-image:
	@$(PODMAN) image exists $(TOOLBOX_TAG) || { \
		printf 'building %s\n' '$(TOOLBOX_TAG)' >&2; \
		$(BOX_ARCHIVE) | $(PODMAN) build --quiet --pull=missing \
			--tag $(TOOLBOX_TAG) \
			--target dev \
			--file containers/toolbox/Containerfile - >/dev/null; \
	}

lock-image:
	@$(PODMAN) image exists $(LOCK_TAG) || { \
		printf 'building %s\n' '$(LOCK_TAG)' >&2; \
		$(BOX_ARCHIVE) | $(PODMAN) build --quiet --pull=missing \
			--tag $(LOCK_TAG) \
			--target lock \
			--file containers/toolbox/Containerfile - >/dev/null; \
	}

live-target-image:
	@$(PODMAN) image exists $(TARGET_TAG) || { \
		printf 'building %s\n' '$(TARGET_TAG)' >&2; \
		$(BOX_ARCHIVE) | $(PODMAN) build --quiet --pull=missing \
			--tag $(TARGET_TAG) \
			--target live-target \
			--file containers/live-target/Containerfile \
			- >/dev/null; \
	}

compatibility-image:
	@$(PODMAN) image exists $(COMPATIBILITY_TAG) || { \
		printf 'building %s\n' '$(COMPATIBILITY_TAG)' >&2; \
		$(BOX_ARCHIVE) | $(PODMAN) build --quiet --pull=missing \
			--tag $(COMPATIBILITY_TAG) \
			--file containers/compatibility/Containerfile - >/dev/null; \
	}

# CI keys its image cache on these, so a cache entry can never outlive the
# context that produced it.
# Emitted as key=value lines so a workflow can consume them directly.
image-key:
	@printf '%s\n' \
		'toolbox=$(TOOLBOX_KEY)' \
		'resolver=$(LOCK_KEY)' \
		'target=$(TARGET_KEY)' \
		'compatibility=$(COMPATIBILITY_KEY)'

# Each archive has its own key and consumer in CI. Save through a temporary file
# so a failed export can never leave an apparently cacheable partial archive.
define save_image
	@mkdir -p $(IMAGE_ARTIFACTS)
	@temporary='$(1).tmp.'$$$$; \
		trap 'rm -f "$$temporary"' EXIT; \
		umask 077; \
		$(PODMAN) save --quiet --format oci-archive '$(2)' > "$$temporary"; \
		test -s "$$temporary"; \
		mv -f "$$temporary" '$(1)'
endef

define load_image
	@test -r '$(1)' || { \
		printf 'cached image archive is missing: %s\n' '$(1)' >&2; exit 1; \
	}
	@$(PODMAN) load --quiet < '$(1)' >/dev/null
	@$(PODMAN) image exists '$(2)' || { \
		printf 'cached archive did not restore expected image: %s\n' '$(2)' >&2; \
		exit 1; \
	}
endef

image-save: image-save-toolbox image-save-resolver image-save-target \
	image-save-compatibility

image-save-toolbox: toolbox-image
	$(call save_image,$(IMAGE_ARTIFACTS)/toolbox.tar,$(TOOLBOX_TAG))

image-save-resolver: lock-image
	$(call save_image,$(IMAGE_ARTIFACTS)/resolver.tar,$(LOCK_TAG))

image-save-target: live-target-image
	$(call save_image,$(IMAGE_ARTIFACTS)/live-target.tar,$(TARGET_TAG))

image-save-compatibility: compatibility-image
	$(call save_image,$(IMAGE_ARTIFACTS)/compatibility.tar,$(COMPATIBILITY_TAG))

image-load: image-load-toolbox image-load-resolver image-load-target \
	image-load-compatibility

image-load-toolbox:
	$(call load_image,$(IMAGE_ARTIFACTS)/toolbox.tar,$(TOOLBOX_TAG))

image-load-resolver:
	$(call load_image,$(IMAGE_ARTIFACTS)/resolver.tar,$(LOCK_TAG))

image-load-target:
	$(call load_image,$(IMAGE_ARTIFACTS)/live-target.tar,$(TARGET_TAG))

image-load-compatibility:
	$(call load_image,$(IMAGE_ARTIFACTS)/compatibility.tar,$(COMPATIBILITY_TAG))

doctor:
	@for required in git sha256sum '$(SYSTEM_PYTHON)' '$(PODMAN)'; do \
		type -P "$$required" >/dev/null 2>&1 || { \
			printf 'required command not found: %s\n' "$$required" >&2; \
			exit 1; \
		}; \
	done
	@$(PODMAN) info >/dev/null 2>&1 || { \
		printf 'podman is unavailable\n' >&2; exit 1; \
	}
	@rootless=$$($(PODMAN) info --format '{{.Host.Security.Rootless}}'); \
		if [[ "$$rootless" != true ]]; then \
			printf 'rootless podman is required, got %s\n' "$$rootless" >&2; \
			exit 1; \
		fi
	@printf '%s\n' \
		"git      $$(git --version)" \
		"python   $$($(SYSTEM_PYTHON) --version 2>&1)" \
		"podman   $$($(PODMAN) version --format '{{.Client.Version}}')" \
		'rootless yes' \
		'toolbox  $(TOOLBOX_TAG)' \
		'resolver $(LOCK_TAG)' \
		'target   $(TARGET_TAG)' \
		'python313 $(COMPATIBILITY_TAG)'

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
