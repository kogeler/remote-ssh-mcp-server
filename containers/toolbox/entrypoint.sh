#!/usr/bin/env bash

# Toolbox entry point.
#
# The host never bind-mounts anything. It streams the work tree in as a tar on
# stdin, and this script unpacks it into a private tmpfs before running the
# requested command. Keeping the logic here rather than on the host means the
# confinement proof travels with the image and stays a lintable shell file.
#
# The container proves its own confinement before touching the archive. A
# container that cannot prove it performs no work.
#
# BOX_EXPORT, when set, holds a space-separated list of paths relative to the
# work tree. The command's stdout is then folded into stderr and stdout carries
# a tar of those paths instead, so results and artefacts never share a channel.
# BOX_EXPORT_ON_SUCCESS=1 suppresses publication after a failed command. Lock
# generation uses it so a partial resolution cannot overwrite the work tree.

set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
    printf 'toolbox: running as root inside the container\n' >&2
    exit 1
fi
if ! grep -Eq '^CapEff:[[:space:]]+0+$' /proc/self/status; then
    printf 'toolbox: effective capabilities are not empty\n' >&2
    exit 1
fi
if ! grep -Eq '^NoNewPrivs:[[:space:]]+1$' /proc/self/status; then
    printf 'toolbox: NoNewPrivs is not set\n' >&2
    exit 1
fi
if ! grep -Eq '^Seccomp:[[:space:]]+2$' /proc/self/status; then
    printf 'toolbox: seccomp filtering is not active\n' >&2
    exit 1
fi

(( $# )) || {
    printf 'toolbox: no command given\n' >&2
    exit 2
}

mkdir -p "$HOME"
chmod 0700 "$HOME"

[[ ! -e /work/src ]] || {
    printf 'toolbox: unexpected pre-existing work tree\n' >&2
    exit 1
}
mkdir -p /work/src /work/out
chmod 0700 /work/src /work/out
tar --extract --file=- --directory=/work/src
cd /work/src

if [[ -z "${BOX_EXPORT:-}" ]]; then
    exec "$@"
fi

status=0
"$@" >&2 || status=$?

if (( status != 0 )) && [[ "${BOX_EXPORT_ON_SUCCESS:-0}" == 1 ]]; then
    exit "$status"
fi

exported=()
for path in ${BOX_EXPORT}; do
    if [[ -e "$path" ]]; then exported+=("$path"); fi
done
if (( ${#exported[@]} )); then
    tar --create --file=- --directory=/work/src -- "${exported[@]}"
fi
exit "$status"
