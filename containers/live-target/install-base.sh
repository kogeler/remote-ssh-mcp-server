#!/usr/bin/env bash

# Install the shared SSH target used by disposable live-test images.

set -euo pipefail

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install --yes \
    --no-install-recommends \
    iproute2 \
    openssh-server \
    procps \
    rsync \
    sudo
rm -rf /var/lib/apt/lists/*

# Debian assigns the OpenSSH privilege-separation account to nogroup (65534),
# which is outside the deliberately small rootless user namespace. Give that
# account its own low system group so pre-authentication setgroups remains
# representable without consuming the host's full subordinate-ID allocation.
if ! getent group sshd >/dev/null; then
    groupadd --system sshd
fi
usermod --gid sshd sshd
sshd_gid="$(id -g sshd)"
test "${sshd_gid}" = "$(getent group sshd | cut -d: -f3)"
((sshd_gid < 2048))

# Rootless execution cannot run PAM's setgid unix_chkpwd helper on every
# supported host kernel. The target authenticates only by its ephemeral SSH
# key, so sudo keeps common-auth for password-required refusal while its
# post-authentication account check is deliberately local and unconditional.
useradd --create-home --uid 1001 --user-group --shell /bin/bash mcp-test
passwd --delete mcp-test
install -d -m 0700 -o mcp-test -g mcp-test /home/mcp-test/.ssh
sed -i \
    's/^@include common-account$/account required pam_permit.so/' \
    /etc/pam.d/sudo
grep -Fx 'account required pam_permit.so' /etc/pam.d/sudo
