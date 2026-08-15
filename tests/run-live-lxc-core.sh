#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf '%s\n' \
        'Usage: run-live-lxc-core.sh --key-mode MODE --public-key PATH --identity-file PATH [options]' \
        '' \
        'Options:' \
        '  --key-mode MODE     fido or ephemeral' \
        '  --image IMAGE       LXC image alias (default: images:debian/13)' \
        '  --preflight-only    Validate prerequisites without creating an instance' \
        '  -h, --help          Show this help'
}

fail() {
    printf 'live-lxc: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

public_key=
identity_file=
key_mode=
image=images:debian/13
preflight_only=0

while (( $# )); do
    case "$1" in
        --key-mode)
            (( $# >= 2 )) || fail '--key-mode requires a value'
            key_mode=$2
            shift 2
            ;;
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

[[ "$key_mode" == fido || "$key_mode" == ephemeral ]] || \
    fail 'provide --key-mode fido or --key-mode ephemeral'
[[ -n "$public_key" ]] || fail 'provide --public-key PATH'
[[ -n "$identity_file" ]] || fail 'provide --identity-file PATH'
[[ "$image" != *[$'\n\r\t ']* ]] || fail 'image alias contains whitespace'

for command in awk base64 chmod find grep install lxc mktemp realpath \
    sed sha256sum ssh ssh-keygen ssh-keyscan stat; do
    require_command "$command"
done

public_key=$(realpath -e -- "$public_key") || fail 'public key cannot be resolved'
identity_file=$(realpath -e -- "$identity_file") || fail 'identity file cannot be resolved'
[[ -f "$public_key" && -r "$public_key" ]] || fail 'public key is not a readable regular file'
[[ -f "$identity_file" && -r "$identity_file" ]] || fail 'identity is not a readable regular file'
[[ "$public_key" != *$'\n'* && "$identity_file" != *$'\n'* ]] || fail 'key paths cannot contain newlines'

key_type=$(awk 'NR == 1 { print $1 }' "$public_key")
case "$key_mode:$key_type" in
    fido:sk-ssh-ed25519@openssh.com|fido:sk-ecdsa-sha2-nistp256@openssh.com) ;;
    ephemeral:ssh-ed25519) ;;
    fido:*) fail 'public key is not a supported OpenSSH FIDO2 key' ;;
    ephemeral:*) fail 'ephemeral mode requires a standard Ed25519 key' ;;
esac
[[ $(awk 'NR == 1 { print NF }' "$public_key") -ge 2 ]] || fail 'public key is malformed'

identity_mode=$(stat -c %a -- "$identity_file")
(( (8#$identity_mode & 077) == 0 )) || fail 'identity file must not be accessible by group or others'

lxc info >/dev/null || fail 'LXC daemon is unavailable'
lxc image info "$image" >/dev/null || fail 'requested LXC image is unavailable'
ssh_path=$(command -v ssh)
[[ -x "$ssh_path" ]] || fail 'OpenSSH client is unavailable'

printf 'live-lxc: preflight passed (mode %s, key type %s, image %s)\n' \
    "$key_mode" "$key_type" "$image" >&2
if (( preflight_only )); then
    exit 0
fi

tool_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python_path=$tool_dir/venv/bin/python
live_runner=$tool_dir/tests/live_lxc_e2e.py
[[ -x "$python_path" ]] || fail 'tool-local venv is missing; run make venv'
[[ -x "$live_runner" ]] || fail 'live MCP runner is missing or not executable'

umask 077
container_name="remote-ssh-mcp-e2e-$(date +%Y%m%d%H%M%S)-$$"
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/remote-ssh-mcp-e2e.XXXXXXXX")
container_created=0

cleanup() {
    local exit_code=$?
    local cleanup_failed=0
    trap - EXIT HUP INT TERM
    set +e
    printf 'live-lxc: cleanup started\n' >&2
    if (( container_created )) && lxc info "$container_name" >/dev/null 2>&1; then
        if ! lxc delete --force "$container_name" >/dev/null 2>&1; then
            printf 'live-lxc: failed to delete test container\n' >&2
            cleanup_failed=1
        fi
    fi
    if [[ -d "$test_dir" ]]; then
        if ! find "$test_dir" -depth -delete; then
            printf 'live-lxc: failed to delete temporary test directory\n' >&2
            cleanup_failed=1
        fi
    fi
    if (( cleanup_failed && exit_code == 0 )); then exit_code=1; fi
    printf 'live-lxc: cleanup complete (status %s)\n' "$exit_code" >&2
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

private_before=$(stat -c '%d:%i:%s:%Y:%a:%U:%G' -- "$identity_file")
public_before=$(sha256sum -- "$public_key" | awk '{ print $1 }')

printf 'live-lxc: creating disposable Debian test target\n' >&2
container_created=1
lxc init "$image" "$container_name" \
    -c security.nesting=true \
    -c security.syscalls.intercept.mknod=true \
    -c security.syscalls.intercept.setxattr=true \
    -c security.idmap.size=1000000 \
    -c security.devlxd=false \
    -c security.idmap.isolated=true \
    -c linux.kernel_modules=br_netfilter >/dev/null
lxc config device override "$container_name" eth0 \
    security.mac_filtering=true \
    security.ipv4_filtering=true \
    security.ipv6_filtering=true >/dev/null
lxc start "$container_name" >/dev/null

state=
for _attempt in $(seq 1 90); do
    state=$(lxc exec "$container_name" -- systemctl is-system-running 2>/dev/null || true)
    if [[ "$state" == running || "$state" == degraded ]]; then
        break
    fi
    sleep 2
done
[[ "$state" == running || "$state" == degraded ]] || fail 'container did not become ready'

for setting in \
    security.nesting=true \
    security.syscalls.intercept.mknod=true \
    security.syscalls.intercept.setxattr=true \
    security.idmap.size=1000000 \
    security.devlxd=false \
    security.idmap.isolated=true \
    linux.kernel_modules=br_netfilter; do
    key=${setting%%=*}
    expected=${setting#*=}
    [[ $(lxc config get "$container_name" "$key") == "$expected" ]] || \
        fail "container setting was not applied: $key"
done
for setting in \
    security.mac_filtering=true \
    security.ipv4_filtering=true \
    security.ipv6_filtering=true; do
    key=${setting%%=*}
    expected=${setting#*=}
    [[ $(lxc config device get "$container_name" eth0 "$key") == "$expected" ]] || \
        fail "container eth0 setting was not applied: $key"
done

printf 'live-lxc: installing isolated target dependencies\n' >&2
lxc exec "$container_name" -- env DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null
lxc exec "$container_name" -- env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y openssh-server rsync sudo iproute2 procps >/dev/null

lxc exec "$container_name" -- useradd --create-home --shell /bin/bash mcp-test
# shellcheck disable=SC2016  # Variables expand in the remote Bash process.
lxc exec "$container_name" -- bash -c \
    'set -euo pipefail; umask 077; password=$(head -c 48 /dev/urandom | base64 -w0); printf "%s" "$password" > /run/remote-ssh-mcp-e2e.password; printf "mcp-test:%s\n" "$password" | chpasswd; unset password'
lxc exec "$container_name" -- install -d -m 0700 -o mcp-test -g mcp-test /home/mcp-test/.ssh
lxc file push "$public_key" "$container_name/home/mcp-test/.ssh/authorized_keys"
lxc exec "$container_name" -- chown mcp-test:mcp-test /home/mcp-test/.ssh/authorized_keys
lxc exec "$container_name" -- chmod 0600 /home/mcp-test/.ssh/authorized_keys
guest_public=$(lxc exec "$container_name" -- sha256sum /home/mcp-test/.ssh/authorized_keys | awk '{ print $1 }')
[[ "$guest_public" == "$public_before" ]] || fail 'authorized key differs from the supplied public key'
[[ $(lxc exec "$container_name" -- id -Gn mcp-test) == mcp-test ]] || fail 'test account has unexpected group access'

lxc exec "$container_name" -- bash -c \
    'set -e; umask 022; printf "%s\n" "PubkeyAuthentication yes" "PasswordAuthentication no" "KbdInteractiveAuthentication no" "PermitEmptyPasswords no" "LogLevel VERBOSE" "AllowUsers mcp-test" > /etc/ssh/sshd_config.d/99-remote-ssh-mcp-e2e.conf'
lxc exec "$container_name" -- bash -c \
    'set -e; umask 022; printf "%s\n" "Defaults:mcp-test timestamp_type=global,timestamp_timeout=5" "mcp-test ALL=(root) NOPASSWD: /bin/bash --noprofile --norc -s" > /etc/sudoers.d/99-remote-ssh-mcp-e2e; chmod 0440 /etc/sudoers.d/99-remote-ssh-mcp-e2e; visudo -cf /etc/sudoers.d/99-remote-ssh-mcp-e2e >/dev/null'
lxc exec "$container_name" -- sshd -t
lxc exec "$container_name" -- systemctl restart ssh
lxc exec "$container_name" -- systemctl is-active --quiet ssh

# shellcheck disable=SC2016  # Variables expand in the remote Bash process.
lxc exec "$container_name" -- bash -c \
    'set -euo pipefail; root=/srv/remote-ssh-mcp-e2e; install -d -m 0750 -o mcp-test -g mcp-test "$root"; printf normal-data > "$root/normal.txt"; : > "$root/empty.txt"; printf "\377\376binary\000tail" > "$root/binary.bin"; printf unusual-data > "$root/space \"quote\" semi; unicode.txt"; printf denied > "$root/denied.txt"; chown -R mcp-test:mcp-test "$root"; chown root:root "$root/denied.txt"; chmod 0600 "$root/denied.txt"; install -d -m 0700 -o root -g root "$root/denied-dir"; printf hidden > "$root/denied-dir/hidden.txt"; chmod 0600 "$root/denied-dir/hidden.txt"; dd if=/dev/urandom of="$root/large-download.bin" bs=1M count=32 status=none; dd if=/dev/urandom of="$root/cancel-download.bin" bs=1M count=128 status=none; chown mcp-test:mcp-test "$root/large-download.bin" "$root/cancel-download.bin"'

ip_address=$(lxc exec "$container_name" -- hostname -I | awk '{ print $1 }')
[[ "$ip_address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'container has no usable IPv4 address'

install -d -m 0700 "$test_dir/bin" "$test_dir/local-root" "$test_dir/local-root/downloads"
lxc file pull "$container_name/etc/ssh/ssh_host_ed25519_key.pub" "$test_dir/host-key.pub"
ssh-keyscan -T 5 -t ed25519 "$ip_address" > "$test_dir/known_hosts" 2> "$test_dir/keyscan.stderr"
chmod 0600 "$test_dir/host-key.pub" "$test_dir/known_hosts" "$test_dir/keyscan.stderr"
expected_host_key=$(ssh-keygen -lf "$test_dir/host-key.pub" | awk '{ print $2 }')
observed_host_key=$(ssh-keygen -lf "$test_dir/known_hosts" | awk '{ print $2 }')
[[ "$expected_host_key" == "$observed_host_key" ]] || fail 'scanned SSH host key does not match the container'

escaped_identity=${identity_file//\\/\\\\}
escaped_identity=${escaped_identity//\"/\\\"}
{
    printf '%s\n' 'Host remote-ssh-mcp-lxc-e2e'
    printf '    HostName %s\n' "$ip_address"
    printf '%s\n' \
        '    Port 22' \
        '    User mcp-test'
    printf '    IdentityFile "%s"\n' "$escaped_identity"
    printf '%s\n' \
        '    IdentitiesOnly yes' \
        '    IdentityAgent none' \
        '    PreferredAuthentications publickey' \
        '    PasswordAuthentication no' \
        '    KbdInteractiveAuthentication no' \
        '    StrictHostKeyChecking yes'
    printf '    UserKnownHostsFile "%s"\n' "$test_dir/known_hosts"
    printf '    HostKeyAlias %s\n' "$ip_address"
    printf '%s\n' \
        '    CheckHostIP no' \
        '    UpdateHostKeys no' \
        '    ControlMaster no' \
        '    ControlPersist no'
} > "$test_dir/ssh_config"
chmod 0600 "$test_dir/ssh_config"

# shellcheck disable=SC2016  # Variables expand when the generated wrapper runs.
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    ': "${REMOTE_SSH_MCP_TEST_SSH_CONFIG:?missing test SSH config}"' \
    ': "${REMOTE_SSH_MCP_TEST_REAL_SSH:?missing real SSH path}"' \
    'exec "$REMOTE_SSH_MCP_TEST_REAL_SSH" -F "$REMOTE_SSH_MCP_TEST_SSH_CONFIG" "$@"' \
    > "$test_dir/bin/ssh"
chmod 0700 "$test_dir/bin/ssh"

export REMOTE_SSH_MCP_TEST_SSH_CONFIG=$test_dir/ssh_config
export REMOTE_SSH_MCP_TEST_REAL_SSH=$ssh_path
settings=$(PATH="$test_dir/bin:$PATH" ssh -G remote-ssh-mcp-lxc-e2e 2>/dev/null)
for expected in \
    "hostname $ip_address" \
    'user mcp-test' \
    'stricthostkeychecking true' \
    'identityagent none' \
    'passwordauthentication no' \
    'kbdinteractiveauthentication no'; do
    grep -Fx -- "$expected" <<< "$settings" >/dev/null || fail "SSH setting is not effective: $expected"
done

accepted_before=$(lxc exec "$container_name" -- bash -c \
    "journalctl -u ssh --no-pager -o cat | grep -c 'Accepted publickey for mcp-test' || true")
[[ "$accepted_before" == 0 ]] || fail 'test target already contains a successful SSH authentication'

if [[ "$key_mode" == fido ]]; then
    printf '%s\n' \
        'live-lxc: target ready; the SSH master will authenticate now.' \
        'live-lxc: confirm the system PIN dialog if shown, then touch the hardware key once.' >&2
    [[ -t 0 ]] || fail 'FIDO live test requires interactive stdin for confirmation'
    printf '%s' 'live-lxc: press Enter when ready to open the SSH master: ' >&2
    IFS= read -r _confirmation
else
    printf '%s\n' \
        'live-lxc: target ready; authenticating with the ephemeral test key.' >&2
fi

REMOTE_SSH_MCP_E2E_CONTAINER=$container_name \
REMOTE_SSH_MCP_E2E_TARGET=remote-ssh-mcp-lxc-e2e \
REMOTE_SSH_MCP_E2E_LOCAL_ROOT=$test_dir/local-root \
REMOTE_SSH_MCP_TEST_SSH_CONFIG=$test_dir/ssh_config \
REMOTE_SSH_MCP_E2E_WRAPPER_DIR=$test_dir/bin \
    "$python_path" "$live_runner"

[[ $(stat -c '%d:%i:%s:%Y:%a:%U:%G' -- "$identity_file") == "$private_before" ]] || \
    fail 'identity file metadata changed during the test'
[[ $(sha256sum -- "$public_key" | awk '{ print $1 }') == "$public_before" ]] || \
    fail 'public key changed during the test'
if grep -F -- "$identity_file" "$test_dir/local-root/server.stderr" >/dev/null || \
    grep -F -- "$public_key" "$test_dir/local-root/server.stderr" >/dev/null; then
    fail 'a supplied key path appeared in server diagnostics'
fi
remote_artifact=
for _attempt in $(seq 1 50); do
    remote_artifact=$(lxc exec "$container_name" -- bash -c \
        "find /tmp -maxdepth 1 -name 'remote-ssh-mcp.*' -print -quit")
    [[ -z "$remote_artifact" ]] && break
    sleep 0.1
done
[[ -z "$remote_artifact" ]] || fail 'remote command runtime artifact remained after the test'

printf 'live-lxc: complete; host key %s; source key files unchanged\n' "$observed_host_key" >&2
