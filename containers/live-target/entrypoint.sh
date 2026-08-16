#!/usr/bin/env bash

# Start the OpenSSH server of the disposable live-test target.
#
# Host keys are generated per container rather than baked into an image layer,
# so no key material is ever stored, shared between runs, or pushed anywhere.
# sshd runs in the foreground and logs to stderr, which makes the container log
# the single authoritative record of every authentication attempt.

set -euo pipefail

install -d -m 0755 /run/sshd
ssh-keygen -A >/dev/null

exec /usr/sbin/sshd -D -e
