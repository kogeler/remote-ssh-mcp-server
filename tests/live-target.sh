#!/usr/bin/env bash
set -euo pipefail

# Live MCP test against a disposable Podman target.
#
# This is the imperative half of the container work: a sequence with retries,
# ownership-verified cleanup, and post-conditions. Everything declarative -
# which image, which confinement, when to rebuild - lives in make/container.mk
# and make/live.mk instead.
#
# Both key modes share this one path. The ephemeral mode generates its own
# Ed25519 key and deletes it; the FIDO mode takes an existing hardware-backed
# key as a runtime parameter and never copies it.

usage() {
    printf '%s\n' \
        'Usage: live-target.sh --image REF [options]' \
        '' \
        'Options:' \
        '  --image REF         Target image built by make' \
        '  --server-image REF  Toolbox image that runs the server in ephemeral mode' \
        '  --mode MODE         ephemeral (default) or fido' \
        '  --public-key PATH   FIDO2 public key, required for --mode fido' \
        '  --identity-file PATH' \
        '                      Matching identity, required for --mode fido' \
        '  --preflight-only    Validate prerequisites without creating anything' \
        '  -h, --help          Show this help'
}

fail() {
    printf 'live: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

image=
server_image=
mode=ephemeral
public_key=
identity_file=
preflight_only=0

while (( $# )); do
    case "$1" in
        --image)
            (( $# >= 2 )) || fail '--image requires a reference'
            image=$2
            shift 2
            ;;
        --server-image)
            (( $# >= 2 )) || fail '--server-image requires a reference'
            server_image=$2
            shift 2
            ;;
        --mode)
            (( $# >= 2 )) || fail '--mode requires a value'
            mode=$2
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

[[ "$mode" == ephemeral || "$mode" == fido ]] || \
    fail 'provide --mode ephemeral or --mode fido'
[[ -n "$image" ]] || fail 'provide --image REF'
[[ "$image" != *[$'\n\r\t ']* ]] || fail 'image reference contains whitespace'

for command in awk base64 chmod find grep install mktemp podman realpath \
    sha256sum ssh ssh-keygen ssh-keyscan stat; do
    require_command "$command"
done

tool_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

umask 077
key_dir=
container_name=
container_created=0
server_name=
server_created=0
network_name=
network_created=0
test_dir=

# The optional --server-image path runs the ephemeral server next to the target
# on a private network. The supported Make target does not select that unfinished
# topology yet, so both public modes currently keep the server on the host and
# publish the target only on a random loopback port. FIDO must remain on the host
# because it needs the hardware key, PIN dialog, and USB device.
containerised_server=0

cleanup() {
    local exit_code=$?
    local cleanup_failed=0 label owner
    trap - EXIT HUP INT TERM
    set +e
    if (( container_created )); then
        # Remove only what this run created, and only after the labels confirm
        # it. A blind force-remove could take somebody else's container.
        label=$(podman container inspect "$container_name" \
            --format '{{index .Config.Labels "remote-ssh-mcp.run"}}' 2>/dev/null)
        owner=$(podman container inspect "$container_name" \
            --format '{{index .Config.Labels "remote-ssh-mcp.owner"}}' 2>/dev/null)
        if [[ "$label" == "$container_name" && "$owner" == live-target ]]; then
            if ! podman rm --force --time 5 "$container_name" >/dev/null 2>&1; then
                printf 'live: failed to remove the test container\n' >&2
                cleanup_failed=1
            fi
        elif [[ -n "$label" || -n "$owner" ]]; then
            printf 'live: refusing to remove %s: labels do not match this run\n' \
                "$container_name" >&2
            cleanup_failed=1
        fi
    fi
    if (( server_created )); then
        if ! podman rm --force --time 5 "$server_name" >/dev/null 2>&1; then
            printf 'live: failed to remove the server container\n' >&2
            cleanup_failed=1
        fi
    fi
    if (( network_created )); then
        if ! podman network rm --force "$network_name" >/dev/null 2>&1; then
            printf 'live: failed to remove the test network\n' >&2
            cleanup_failed=1
        fi
    fi
    for directory in "$test_dir" "$key_dir"; do
        if [[ -n "$directory" && -d "$directory" ]]; then
            if ! find "$directory" -depth -delete; then
                printf 'live: failed to delete %s\n' "$directory" >&2
                cleanup_failed=1
            fi
        fi
    done
    if (( cleanup_failed && exit_code == 0 )); then exit_code=1; fi
    printf 'live: cleanup complete (status %s)\n' "$exit_code" >&2
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$mode" == ephemeral ]]; then
    [[ -z "$public_key" && -z "$identity_file" ]] || \
        fail 'ephemeral mode generates its own key; do not pass key paths'
    key_dir=$(mktemp -d "${TMPDIR:-/tmp}/remote-ssh-mcp-live-key.XXXXXXXX")
    identity_file=$key_dir/id_ed25519
    public_key=$identity_file.pub
    ssh-keygen -q -t ed25519 -N '' -C remote-ssh-mcp-ephemeral-live-test \
        -f "$identity_file"
    chmod 0600 "$identity_file" "$public_key"
else
    [[ -n "$public_key" ]] || fail 'fido mode requires --public-key PATH'
    [[ -n "$identity_file" ]] || fail 'fido mode requires --identity-file PATH'
fi

public_key=$(realpath -e -- "$public_key") || fail 'public key cannot be resolved'
identity_file=$(realpath -e -- "$identity_file") || \
    fail 'identity file cannot be resolved'
[[ -f "$public_key" && -r "$public_key" ]] || \
    fail 'public key is not a readable regular file'
[[ -f "$identity_file" && -r "$identity_file" ]] || \
    fail 'identity is not a readable regular file'
[[ "$public_key" != *$'\n'* && "$identity_file" != *$'\n'* ]] || \
    fail 'key paths cannot contain newlines'

key_type=$(awk 'NR == 1 { print $1 }' "$public_key")
case "$mode:$key_type" in
    fido:sk-ssh-ed25519@openssh.com|fido:sk-ecdsa-sha2-nistp256@openssh.com) ;;
    ephemeral:ssh-ed25519) ;;
    fido:*) fail 'public key is not a supported OpenSSH FIDO2 key' ;;
    ephemeral:*) fail 'ephemeral mode requires a standard Ed25519 key' ;;
esac
[[ $(awk 'NR == 1 { print NF }' "$public_key") -ge 2 ]] || \
    fail 'public key is malformed'

identity_mode=$(stat -c %a -- "$identity_file")
(( (8#$identity_mode & 077) == 0 )) || \
    fail 'identity file must not be accessible by group or others'

podman info >/dev/null 2>&1 || fail 'Podman is unavailable'
# Rootless is the isolation boundary, not a preference: under a rootful Podman
# the target's root would be the host's root.
rootless=$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo false)
[[ "$rootless" == true ]] || \
    fail "rootless Podman is required, got rootless=${rootless}"
ssh_path=$(command -v ssh)
[[ -x "$ssh_path" ]] || fail 'OpenSSH client is unavailable'

key_fingerprint=$(ssh-keygen -lf "$public_key" | awk '{ print $2 }')
printf 'live: preflight passed (mode %s, key %s, fingerprint %s)\n' \
    "$mode" "$key_type" "$key_fingerprint" >&2
if (( preflight_only )); then
    exit 0
fi

podman image exists "$image" || fail "target image is missing: ${image}"
if [[ -n "$server_image" ]]; then
    [[ "$mode" == ephemeral ]] || \
        fail 'the FIDO mode needs the hardware key on this host, not in a container'
    podman image exists "$server_image" || \
        fail "server image is missing: ${server_image}"
    containerised_server=1
fi

python_path=$tool_dir/venv-runtime/bin/python
live_runner=$tool_dir/tests/live_podman_e2e.py
[[ -x "$python_path" ]] || fail 'runtime venv is missing; run make runtime-venv'
[[ -x "$live_runner" ]] || fail 'live MCP runner is missing or not executable'

container_name="remote-ssh-mcp-e2e-$(date +%Y%m%d%H%M%S)-$$"
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/remote-ssh-mcp-e2e.XXXXXXXX")

private_before=$(stat -c '%d:%i:%s:%Y:%a:%U:%G' -- "$identity_file")
public_before=$(sha256sum -- "$public_key" | awk '{ print $1 }')

if (( containerised_server )); then
    network_name="remote-ssh-mcp-e2e-net-$(date +%Y%m%d%H%M%S)-$$"
    printf 'live: creating the private test network\n' >&2
    network_created=1
    podman network create \
        --label "remote-ssh-mcp.run=${network_name}" \
        --label remote-ssh-mcp.owner=live-target \
        "$network_name" >/dev/null
fi

# The current host-server topology publishes only on loopback. The optional
# containerised path instead reaches the target by name on its private network.
target_network=(--publish 127.0.0.1::22)
if (( containerised_server )); then
    target_network=(--network "$network_name" --network-alias live-target)
fi

printf 'live: creating the disposable test container\n' >&2
container_created=1
# The target needs root inside its user namespace to run sshd, manage the test
# account, and honour sudo. Rootless Podman already maps that root to the
# unprivileged invoking user, so the capability list below never leaves the
# namespace.
#
# Three hardening options are deliberately absent, and none should be added:
#
#   --userns=auto        sshd's privilege separation calls setgroups() with a
#                        gid outside a narrow auto-allocated map and fails with
#                        EINVAL before any key exchange completes. The toolbox
#                        does use it; this target cannot.
#   --security-opt=no-new-privileges
#                        would neutralise the setuid sudo binary, and
#                        passwordless sudo is part of the tested contract.
#   --read-only          sshd writes to /run and the matrix installs a sudo
#                        policy per case.
podman create \
    --name "$container_name" \
    --hostname remote-ssh-mcp-e2e \
    --label "remote-ssh-mcp.run=${container_name}" \
    --label remote-ssh-mcp.owner=live-target \
    --pull=never \
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
    --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
    "${target_network[@]}" \
    "$image" >/dev/null

install -d -m 0700 "$test_dir/bin" "$test_dir/local-root" \
    "$test_dir/local-root/downloads"
podman cp "$public_key" "$container_name:/home/mcp-test/.ssh/authorized_keys"
podman start "$container_name" >/dev/null

listening=0
for _attempt in $(seq 1 90); do
    if podman exec "$container_name" \
        ss -Hltn 'sport = :22' 2>/dev/null | grep -q LISTEN; then
        listening=1
        break
    fi
    sleep 1
done
(( listening )) || fail 'the target SSH server did not start'

podman exec "$container_name" \
    chown mcp-test:mcp-test /home/mcp-test/.ssh/authorized_keys
podman exec "$container_name" chmod 0600 /home/mcp-test/.ssh/authorized_keys
guest_public=$(podman exec "$container_name" \
    sha256sum /home/mcp-test/.ssh/authorized_keys | awk '{ print $1 }')
[[ "$guest_public" == "$public_before" ]] || \
    fail 'authorized key differs from the supplied public key'
[[ $(podman exec "$container_name" id -Gn mcp-test) == mcp-test ]] || \
    fail 'test account has unexpected group access'

# Podman rewrites --cap-drop=ALL into a delta against its own default set, so
# the declared configuration proves nothing. Verify the mask the kernel granted.
capability_mask=$(podman exec "$container_name" \
    awk '/^CapEff:/ { print $2 }' /proc/self/status)
[[ "$capability_mask" =~ ^[0-9a-fA-F]+$ ]] || \
    fail 'cannot read the target capability mask'
for forbidden in \
    'CAP_DAC_READ_SEARCH=2' \
    'CAP_NET_RAW=13' \
    'CAP_SYS_MODULE=16' \
    'CAP_SYS_RAWIO=17' \
    'CAP_SYS_PTRACE=19' \
    'CAP_SYS_ADMIN=21' \
    'CAP_SYS_BOOT=22' \
    'CAP_SYS_TIME=25' \
    'CAP_MKNOD=27' \
    'CAP_SETFCAP=31'; do
    name=${forbidden%%=*}
    bit=${forbidden#*=}
    (( ((16#$capability_mask >> bit) & 1) == 0 )) || \
        fail "target holds a capability it must not have: $name"
done
for required in 'CAP_SETUID=7' 'CAP_NET_ADMIN=12'; do
    name=${required%%=*}
    bit=${required#*=}
    (( ((16#$capability_mask >> bit) & 1) == 1 )) || \
        fail "target is missing a capability the matrix needs: $name"
done
[[ $(podman inspect --format '{{.HostConfig.Privileged}}' "$container_name") \
    == false ]] || fail 'target container is privileged'
[[ $(podman inspect --format '{{.HostConfig.PidsLimit}}' "$container_name") \
    == 512 ]] || fail 'target process limit was not applied'

# shellcheck disable=SC2016  # Variables expand in the target Bash process.
podman exec "$container_name" bash -c \
    'set -euo pipefail; umask 077; password=$(head -c 48 /dev/urandom | base64 -w0); printf "%s" "$password" > /run/remote-ssh-mcp-e2e.password; printf "mcp-test:%s\n" "$password" | chpasswd; unset password'

# shellcheck disable=SC2016  # Variables expand in the target Bash process.
podman exec "$container_name" bash -c \
    'set -e; umask 022; printf "%s\n" "Defaults:mcp-test timestamp_type=global,timestamp_timeout=5" "mcp-test ALL=(root) NOPASSWD: /bin/bash --noprofile --norc -s" > /etc/sudoers.d/99-remote-ssh-mcp-e2e; chmod 0440 /etc/sudoers.d/99-remote-ssh-mcp-e2e; visudo -cf /etc/sudoers.d/99-remote-ssh-mcp-e2e >/dev/null'

# shellcheck disable=SC2016  # Variables expand in the target Bash process.
podman exec "$container_name" bash -c \
    'set -euo pipefail; root=/srv/remote-ssh-mcp-e2e; install -d -m 0750 -o mcp-test -g mcp-test "$root"; printf normal-data > "$root/normal.txt"; : > "$root/empty.txt"; printf "\377\376binary\000tail" > "$root/binary.bin"; printf unusual-data > "$root/space \"quote\" semi; unicode.txt"; printf denied > "$root/denied.txt"; chown -R mcp-test:mcp-test "$root"; chown root:root "$root/denied.txt"; chmod 0600 "$root/denied.txt"; install -d -m 0700 -o root -g root "$root/denied-dir"; printf hidden > "$root/denied-dir/hidden.txt"; chmod 0600 "$root/denied-dir/hidden.txt"; dd if=/dev/urandom of="$root/large-download.bin" bs=1M count=32 status=none; dd if=/dev/urandom of="$root/cancel-download.bin" bs=1M count=128 status=none; chown mcp-test:mcp-test "$root/large-download.bin" "$root/cancel-download.bin"'

podman exec "$container_name" cat /etc/ssh/ssh_host_ed25519_key.pub \
    > "$test_dir/host-key.pub"
chmod 0600 "$test_dir/host-key.pub"
expected_host_key=$(ssh-keygen -lf "$test_dir/host-key.pub" | awk '{ print $2 }')

if (( containerised_server )); then
    # No port is published, so there is nothing to scan from here. The key is
    # taken from the target itself over the podman channel, which is a stronger
    # source than a scan: it cannot be answered by anything else.
    ssh_host=live-target
    ssh_port=22
    printf '%s %s\n' "$ssh_host" "$(cat "$test_dir/host-key.pub")" \
        > "$test_dir/known_hosts"
    chmod 0600 "$test_dir/known_hosts"
    observed_host_key=$expected_host_key
else
    published=$(podman port "$container_name" 22/tcp | head -n 1)
    [[ "$published" == 127.0.0.1:* ]] || \
        fail 'the target SSH port is not published on the loopback address'
    ssh_port=${published##*:}
    [[ "$ssh_port" =~ ^[0-9]+$ ]] || fail 'cannot determine the published SSH port'
    ssh_host=127.0.0.1
    ssh-keyscan -p "$ssh_port" -T 5 -t ed25519 "$ssh_host" \
        > "$test_dir/known_hosts" 2> "$test_dir/keyscan.stderr"
    chmod 0600 "$test_dir/known_hosts" "$test_dir/keyscan.stderr"
    observed_host_key=$(ssh-keygen -lf "$test_dir/known_hosts" | awk '{ print $2 }')
    [[ "$expected_host_key" == "$observed_host_key" ]] || \
        fail 'scanned SSH host key does not match the container'
fi

config_identity=$identity_file
if (( containerised_server )); then config_identity=/home/box/.ssh/id_ed25519; fi
escaped_identity=${config_identity//\\/\\\\}
escaped_identity=${escaped_identity//\"/\\\"}
config_known_hosts=$test_dir/known_hosts
if (( containerised_server )); then
    config_known_hosts=/home/box/.ssh/known_hosts
fi
{
    printf '%s\n' 'Host remote-ssh-mcp-podman-e2e'
    printf '    HostName %s\n' "$ssh_host"
    printf '    Port %s\n' "$ssh_port"
    printf '%s\n' '    User mcp-test'
    printf '    IdentityFile "%s"\n' "$escaped_identity"
    printf '%s\n' \
        '    IdentitiesOnly yes' \
        '    IdentityAgent none' \
        '    PreferredAuthentications publickey' \
        '    PasswordAuthentication no' \
        '    KbdInteractiveAuthentication no' \
        '    StrictHostKeyChecking yes'
    printf '    UserKnownHostsFile "%s"\n' "$config_known_hosts"
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

if (( containerised_server )); then
    server_name="remote-ssh-mcp-server-$(date +%Y%m%d%H%M%S)-$$"
    printf 'live: creating the server container\n' >&2
    server_created=1
    # The server under test is an ordinary unprivileged process: no added
    # capabilities, no privilege escalation, and no way back to the host. It
    # reads its SSH configuration from the home directory, so it needs no
    # wrapper on PATH.
    podman create \
        --name "$server_name" \
        --label "remote-ssh-mcp.run=${server_name}" \
        --label remote-ssh-mcp.owner=live-server \
        --pull=never \
        --interactive \
        --network "$network_name" \
        --entrypoint '["/usr/bin/tini", "--", "/usr/local/bin/python"]' \
        --workdir /work/local-root \
        --cap-drop=ALL \
        --security-opt=no-new-privileges \
        --ipc=private \
        --pid=private \
        --uts=private \
        --cgroupns=private \
        --systemd=false \
        --pids-limit=1024 \
        --memory=2g \
        --memory-swap=2g \
        --log-driver=k8s-file \
        --env HOME=/home/box \
        --env PYTHONPATH=/work/src \
        --env LANG=C.UTF-8 \
        --env LC_ALL=C.UTF-8 \
        --env TZ=UTC \
        "$server_image" \
        /work/src/remote-ssh-mcp.py \
        --local-root /work/local-root \
        --connect-timeout 300 \
        --command-timeout 10 \
        --max-output-bytes 4096 \
        --max-transfers 2 \
        --log-level INFO >/dev/null

    # Everything the server needs is copied in before it starts. Nothing is
    # bind-mounted, so the container never sees the host filesystem.
    # podman cp copies the *contents* of a directory into an existing
    # destination and refuses to create a missing one, so the staging tree
    # mirrors the destination and each side is copied in a single call.
    staging=$test_dir/server-stage
    install -d -m 0700 "$staging" "$staging/home/.ssh" "$staging/work/src" \
        "$staging/work/local-root" "$staging/work/local-root/downloads"
    cp -- "$test_dir/ssh_config" "$staging/home/.ssh/config"
    cp -- "$test_dir/known_hosts" "$staging/home/.ssh/known_hosts"
    cp -- "$identity_file" "$staging/home/.ssh/id_ed25519"
    chmod 0600 "$staging/home/.ssh/config" "$staging/home/.ssh/known_hosts" \
        "$staging/home/.ssh/id_ed25519"
    cp -- "$tool_dir/remote-ssh-mcp.py" "$staging/work/src/remote-ssh-mcp.py"
    cp -r -- "$tool_dir/remote_ssh_mcp" "$staging/work/src/remote_ssh_mcp"
    chmod 0755 "$staging/work/src/remote-ssh-mcp.py"
    podman cp "$staging/home" "$server_name:/home/box"
    podman cp "$staging/work" "$server_name:/work"
fi

export REMOTE_SSH_MCP_TEST_SSH_CONFIG=$test_dir/ssh_config
export REMOTE_SSH_MCP_TEST_REAL_SSH=$ssh_path
settings=$(PATH="$test_dir/bin:$PATH" ssh -G remote-ssh-mcp-podman-e2e 2>/dev/null)
for expected in \
    "hostname $ssh_host" \
    "port $ssh_port" \
    'user mcp-test' \
    'stricthostkeychecking true' \
    'identityagent none' \
    'passwordauthentication no' \
    'kbdinteractiveauthentication no'; do
    grep -Fx -- "$expected" <<< "$settings" >/dev/null || \
        fail "SSH setting is not effective: $expected"
done

accepted_before=$(podman logs "$container_name" 2>&1 \
    | grep -c 'Accepted publickey for mcp-test' || true)
[[ "$accepted_before" == 0 ]] || \
    fail 'test target already contains a successful SSH authentication'

if [[ "$mode" == fido ]]; then
    printf '%s\n' \
        'live: target ready; the SSH master will authenticate now.' \
        'live: confirm the system PIN dialog if shown, then touch the key once.' >&2
    [[ -t 0 ]] || fail 'FIDO live test requires interactive stdin for confirmation'
    printf '%s' 'live: press Enter when ready to open the SSH master: ' >&2
    IFS= read -r _confirmation
else
    printf 'live: target ready; authenticating with the ephemeral test key.\n' >&2
fi

driver_environment=(
    "REMOTE_SSH_MCP_E2E_CONTAINER=$container_name"
    REMOTE_SSH_MCP_E2E_TARGET=remote-ssh-mcp-podman-e2e
)
if (( containerised_server )); then
    driver_environment+=(
        "REMOTE_SSH_MCP_E2E_SERVER_CONTAINER=$server_name"
        REMOTE_SSH_MCP_E2E_LOCAL_ROOT=/work/local-root
        "REMOTE_SSH_MCP_E2E_STDERR=$test_dir/server.stderr"
    )
else
    driver_environment+=(
        "REMOTE_SSH_MCP_E2E_LOCAL_ROOT=$test_dir/local-root"
        "REMOTE_SSH_MCP_TEST_SSH_CONFIG=$test_dir/ssh_config"
        "REMOTE_SSH_MCP_E2E_WRAPPER_DIR=$test_dir/bin"
    )
fi
env "${driver_environment[@]}" "$python_path" "$live_runner"

[[ $(stat -c '%d:%i:%s:%Y:%a:%U:%G' -- "$identity_file") == "$private_before" ]] || \
    fail 'identity file metadata changed during the test'
[[ $(sha256sum -- "$public_key" | awk '{ print $1 }') == "$public_before" ]] || \
    fail 'public key changed during the test'
server_diagnostics=$test_dir/local-root/server.stderr
if (( containerised_server )); then server_diagnostics=$test_dir/server.stderr; fi
if grep -F -- "$identity_file" "$server_diagnostics" >/dev/null || \
    grep -F -- "$public_key" "$server_diagnostics" >/dev/null; then
    fail 'a supplied key path appeared in server diagnostics'
fi
remote_artifact=
for _attempt in $(seq 1 50); do
    remote_artifact=$(podman exec "$container_name" bash -c \
        "find /tmp -maxdepth 1 -name 'remote-ssh-mcp.*' -print -quit")
    [[ -z "$remote_artifact" ]] && break
    sleep 0.1
done
[[ -z "$remote_artifact" ]] || \
    fail 'remote command runtime artifact remained after the test'

printf 'live: complete; host key %s; source key files unchanged\n' \
    "$observed_host_key" >&2
