"""Disposable Podman network and SSH-target provisioning."""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

from .process import (
    OWNER_LABEL,
    RUN_LABEL,
    LiveFailure,
    LiveResources,
    checked,
    podman_exec,
    podman_output,
    run_process,
)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{secrets.token_hex(4)}"


def create_internal_network(resources: LiveResources) -> str:
    name = unique_name("remote-ssh-mcp-e2e-net")
    resources.network_name = name
    print("live: creating the private test network", file=sys.stderr)
    checked(
        [
            resources.podman,
            "network",
            "create",
            "--internal",
            "--label",
            f"{RUN_LABEL}={name}",
            "--label",
            f"{OWNER_LABEL}=live-network",
            name,
        ],
        "creating the private network",
    )
    internal = podman_output(
        resources,
        "network",
        "inspect",
        "--format",
        "{{.Internal}}",
        name,
        purpose="inspecting the private network",
    )
    if internal != "true":
        raise LiveFailure("the automatic live network is not internal")
    return name


def wait_for_sshd(resources: LiveResources, target: str) -> None:
    for _attempt in range(90):
        completed = run_process(
            [
                resources.podman,
                "exec",
                target,
                "ss",
                "-Hltn",
                "sport = :22",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0 and "LISTEN" in completed.stdout:
            return
        time.sleep(1)
    raise LiveFailure("the target SSH server did not start")


def verify_target_confinement(
    resources: LiveResources, target: str, network: str | None
) -> None:
    capability_text = (
        podman_exec(
            resources,
            target,
            "awk",
            "/^CapEff:/ { print $2 }",
            "/proc/self/status",
            purpose="reading target capabilities",
        )
        .decode()
        .strip()
    )
    try:
        capability_mask = int(capability_text, 16)
    except ValueError as error:
        raise LiveFailure("cannot read the target capability mask") from error

    forbidden = {
        "CAP_DAC_READ_SEARCH": 2,
        "CAP_NET_RAW": 13,
        "CAP_SYS_MODULE": 16,
        "CAP_SYS_RAWIO": 17,
        "CAP_SYS_PTRACE": 19,
        "CAP_SYS_ADMIN": 21,
        "CAP_SYS_BOOT": 22,
        "CAP_SYS_TIME": 25,
        "CAP_MKNOD": 27,
        "CAP_SETFCAP": 31,
    }
    for name, bit in forbidden.items():
        if capability_mask >> bit & 1:
            raise LiveFailure(f"target holds a capability it must not have: {name}")
    for name, bit in {"CAP_SETUID": 7, "CAP_NET_ADMIN": 12}.items():
        if not capability_mask >> bit & 1:
            raise LiveFailure(f"target is missing a required capability: {name}")

    privileged = podman_output(
        resources,
        "inspect",
        "--format",
        "{{.HostConfig.Privileged}}",
        target,
        purpose="inspecting target privilege mode",
    )
    pids_limit = podman_output(
        resources,
        "inspect",
        "--format",
        "{{.HostConfig.PidsLimit}}",
        target,
        purpose="inspecting target process limit",
    )
    if privileged != "false" or pids_limit != "512":
        raise LiveFailure("target confinement does not match the declared policy")

    if network is not None:
        published = run_process(
            [resources.podman, "port", target, "22/tcp"],
            text=True,
            capture_output=True,
            check=False,
        )
        if published.stdout.strip():
            raise LiveFailure("the automatic target published its SSH port")
        attached = podman_output(
            resources,
            "inspect",
            "--format",
            "{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}",
            target,
            purpose="inspecting target networks",
        ).split()
        if attached != [network]:
            raise LiveFailure("the target is not isolated to the per-run network")


def provision_target(
    resources: LiveResources,
    image: str,
    policy: list[str],
    public_key: Path,
    public_digest: str,
    network: str | None,
) -> str:
    name = unique_name("remote-ssh-mcp-e2e")
    resources.target_name = name
    network_options = ["--publish", "127.0.0.1::22"]
    if network is not None:
        network_options = ["--network", network, "--network-alias", "live-target"]
    print("live: creating the disposable test container", file=sys.stderr)
    checked(
        [
            resources.podman,
            "create",
            "--name",
            name,
            "--hostname",
            "remote-ssh-mcp-e2e",
            "--label",
            f"{RUN_LABEL}={name}",
            "--label",
            f"{OWNER_LABEL}=live-target",
            *policy,
            *network_options,
            image,
        ],
        "creating the target container",
    )
    checked(
        [
            resources.podman,
            "cp",
            str(public_key),
            f"{name}:/home/mcp-test/.ssh/authorized_keys",
        ],
        "installing the target public key",
    )
    checked([resources.podman, "start", name], "starting the target container")
    wait_for_sshd(resources, name)
    podman_exec(
        resources,
        name,
        "chown",
        "mcp-test:mcp-test",
        "/home/mcp-test/.ssh/authorized_keys",
        purpose="setting target key ownership",
    )
    podman_exec(
        resources,
        name,
        "chmod",
        "0600",
        "/home/mcp-test/.ssh/authorized_keys",
        purpose="setting target key permissions",
    )
    guest_digest = (
        podman_exec(
            resources,
            name,
            "sha256sum",
            "/home/mcp-test/.ssh/authorized_keys",
            purpose="hashing the installed target key",
        )
        .decode()
        .split()[0]
    )
    if guest_digest != public_digest:
        raise LiveFailure("authorized key differs from the supplied public key")
    groups = (
        podman_exec(
            resources,
            name,
            "id",
            "-Gn",
            "mcp-test",
            purpose="checking the target account",
        )
        .decode()
        .strip()
    )
    if groups != "mcp-test":
        raise LiveFailure("test account has unexpected group access")
    verify_target_confinement(resources, name, network)
    return name


def provision_target_fixtures(resources: LiveResources, target: str) -> None:
    password_script = (
        "set -euo pipefail; umask 077; "
        "password=$(head -c 48 /dev/urandom | base64 -w0); "
        'printf "%s" "$password" > /run/remote-ssh-mcp-e2e.password; '
        'printf "mcp-test:%s\\n" "$password" | chpasswd; unset password'
    )
    podman_exec(
        resources,
        target,
        "bash",
        "-c",
        password_script,
        purpose="preparing the target password fixture",
    )
    sudo_script = (
        "set -e; umask 022; "
        'printf "%s\\n" '
        '"Defaults:mcp-test timestamp_type=global,timestamp_timeout=5" '
        '"mcp-test ALL=(root) NOPASSWD: /bin/bash --noprofile --norc -s" '
        "> /etc/sudoers.d/99-remote-ssh-mcp-e2e; "
        "chmod 0440 /etc/sudoers.d/99-remote-ssh-mcp-e2e; "
        "visudo -cf /etc/sudoers.d/99-remote-ssh-mcp-e2e >/dev/null"
    )
    podman_exec(
        resources,
        target,
        "bash",
        "-c",
        sudo_script,
        purpose="preparing the target sudo policy",
    )
    fixture_script = (
        "set -euo pipefail; root=/srv/remote-ssh-mcp-e2e; "
        'install -d -m 0750 -o mcp-test -g mcp-test "$root"; '
        'printf normal-data > "$root/normal.txt"; : > "$root/empty.txt"; '
        'printf "\\377\\376binary\\000tail" > "$root/binary.bin"; '
        'printf unusual-data > "$root/space \\"quote\\" semi; unicode.txt"; '
        'printf denied > "$root/denied.txt"; chown -R mcp-test:mcp-test "$root"; '
        'chown root:root "$root/denied.txt"; chmod 0600 "$root/denied.txt"; '
        'install -d -m 0700 -o root -g root "$root/denied-dir"; '
        'printf hidden > "$root/denied-dir/hidden.txt"; '
        'chmod 0600 "$root/denied-dir/hidden.txt"; '
        'dd if=/dev/urandom of="$root/large-download.bin" bs=1M count=32 status=none; '
        'dd if=/dev/urandom of="$root/cancel-download.bin" bs=1M count=128 status=none; '
        'chown mcp-test:mcp-test "$root/large-download.bin" '
        '"$root/cancel-download.bin"'
    )
    podman_exec(
        resources,
        target,
        "bash",
        "-c",
        fixture_script,
        purpose="preparing target filesystem fixtures",
    )
