#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf '%s\n' \
        'Usage: run-live-fido-lxc.sh --public-key PATH --identity-file PATH [options]' \
        '' \
        'Options:' \
        '  --public-key PATH   OpenSSH FIDO2 public key' \
        '  --identity-file PATH' \
        '                      Matching protected OpenSSH identity' \
        '  --image IMAGE       LXC image alias (default: images:debian/13)' \
        '  --preflight-only    Validate prerequisites without creating an instance' \
        '  -h, --help          Show this help'
}

fail() {
    printf 'live-fido: %s\n' "$*" >&2
    exit 1
}

public_key=
identity_file=
image=images:debian/13
preflight_only=0

while (( $# )); do
    case "$1" in
        --public-key)
            (( $# >= 2 )) || fail '--public-key requires a path'
            public_key=$2
            shift 2
            ;;
        --identity-file)
            (( $# >= 2 )) || fail '--identity-file requires a path'
            identity_file=$2
            shift 2
            ;;
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

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
core="${script_dir}/run-live-lxc-core.sh"

if [[ ! -x "$core" ]]; then
    printf 'live-fido: core harness is missing or not executable: %s\n' "$core" >&2
    exit 1
fi

arguments=(
    --key-mode fido
    --public-key "$public_key"
    --identity-file "$identity_file"
    --image "$image"
)
if (( preflight_only )); then arguments+=(--preflight-only); fi

exec "$core" "${arguments[@]}"
