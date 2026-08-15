#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf '%s\n' \
        'Usage: run-live-lxc.sh [options]' \
        '' \
        'Options:' \
        '  --image IMAGE       LXC image alias (default: images:debian/13)' \
        '  --preflight-only    Validate prerequisites without creating an instance' \
        '  -h, --help          Show this help'
}

fail() {
    printf 'live-lxc: %s\n' "$*" >&2
    exit 1
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    usage
    exit 0
fi

image=images:debian/13
preflight_only=0
while (( $# )); do
    case "$1" in
        --image)
            (( $# >= 2 )) || fail '--image requires an alias'
            image=$2
            shift 2
            ;;
        --preflight-only)
            preflight_only=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

for command in chmod find mktemp ssh-keygen; do
    command -v "$command" >/dev/null 2>&1 || \
        fail "required command not found: $command"
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
core="${script_dir}/run-live-lxc-core.sh"
[[ -x "$core" ]] || fail "core harness is missing or not executable: $core"

umask 077
key_dir=$(mktemp -d "${TMPDIR:-/tmp}/remote-ssh-mcp-live-key.XXXXXXXX")
identity_file=$key_dir/id_ed25519
public_key=$identity_file.pub

cleanup() {
    local exit_code=$?
    trap - EXIT HUP INT TERM
    set +e
    if [[ -d "$key_dir" ]]; then find "$key_dir" -depth -delete; fi
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

ssh-keygen -q -t ed25519 -N '' -C remote-ssh-mcp-ephemeral-live-test \
    -f "$identity_file"
chmod 0600 "$identity_file" "$public_key"

arguments=(
    --key-mode ephemeral
    --public-key "$public_key"
    --identity-file "$identity_file"
    --image "$image"
)
if (( preflight_only )); then arguments+=(--preflight-only); fi

"$core" "${arguments[@]}"
