"""SSH connection material and confined live-server provisioning."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

from .process import (
    OWNER_LABEL,
    RUN_LABEL,
    SERVER_HOME,
    TARGET_ALIAS,
    ConnectionFiles,
    KeyMaterial,
    LiveFailure,
    LiveResources,
    checked,
    key_fingerprint,
    podman_exec,
    podman_output,
    require_program,
    run_process,
)
from .topology import unique_name


def write_ssh_config(
    path: Path,
    *,
    host: str,
    port: int,
    identity: Path,
    known_hosts: Path,
) -> None:
    escaped_identity = str(identity).replace("\\", "\\\\").replace('"', '\\"')
    path.write_text(
        "\n".join(
            (
                f"Host {TARGET_ALIAS}",
                f"    HostName {host}",
                f"    Port {port}",
                "    User mcp-test",
                f'    IdentityFile "{escaped_identity}"',
                "    IdentitiesOnly yes",
                "    IdentityAgent none",
                "    PreferredAuthentications publickey",
                "    PasswordAuthentication no",
                "    KbdInteractiveAuthentication no",
                "    StrictHostKeyChecking yes",
                f'    UserKnownHostsFile "{known_hosts}"',
                "    CheckHostIP no",
                "    UpdateHostKeys no",
                "    ControlMaster no",
                "    ControlPersist no",
                "",
            )
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def prepare_connection_files(
    resources: LiveResources,
    target: str,
    key: KeyMaterial,
    test_dir: Path,
    containerised_server: bool,
) -> ConnectionFiles:
    host_key = test_dir / "host-key.pub"
    host_key.write_bytes(
        podman_exec(
            resources,
            target,
            "cat",
            "/etc/ssh/ssh_host_ed25519_key.pub",
            purpose="reading the target host key",
        )
    )
    host_key.chmod(0o600)
    expected = key_fingerprint(host_key)
    known_hosts = test_dir / "known_hosts"

    if containerised_server:
        ssh_host = "live-target"
        ssh_port = 22
        known_hosts.write_text(
            f"{ssh_host} {host_key.read_text(encoding='utf-8').strip()}\n",
            encoding="utf-8",
        )
        observed = expected
        identity = SERVER_HOME / ".ssh/id_ed25519"
        config_known_hosts = SERVER_HOME / ".ssh/known_hosts"
    else:
        published = podman_output(
            resources,
            "port",
            target,
            "22/tcp",
            purpose="reading the target SSH port",
        )
        if not published.startswith("127.0.0.1:"):
            raise LiveFailure("the target SSH port is not published on loopback")
        ssh_host = "127.0.0.1"
        try:
            ssh_port = int(published.rsplit(":", 1)[1])
        except ValueError as error:
            raise LiveFailure("cannot determine the published SSH port") from error
        scan = checked(
            [
                require_program("ssh-keyscan"),
                "-p",
                str(ssh_port),
                "-T",
                "5",
                "-t",
                "ed25519",
                ssh_host,
            ],
            "scanning the target host key",
        )
        assert isinstance(scan.stdout, bytes)
        known_hosts.write_bytes(scan.stdout)
        observed = key_fingerprint(known_hosts)
        if observed != expected:
            raise LiveFailure("scanned SSH host key does not match the container")
        identity = key.identity_file
        config_known_hosts = known_hosts
    known_hosts.chmod(0o600)

    ssh_config = test_dir / "ssh_config"
    write_ssh_config(
        ssh_config,
        host=ssh_host,
        port=ssh_port,
        identity=identity,
        known_hosts=config_known_hosts,
    )
    wrapper_dir = test_dir / "bin"
    wrapper_dir.mkdir(mode=0o700)
    wrapper = wrapper_dir / "ssh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        ': "${REMOTE_SSH_MCP_TEST_SSH_CONFIG:?missing test SSH config}"\n'
        ': "${REMOTE_SSH_MCP_TEST_REAL_SSH:?missing real SSH path}"\n'
        'exec "$REMOTE_SSH_MCP_TEST_REAL_SSH" '
        '-F "$REMOTE_SSH_MCP_TEST_SSH_CONFIG" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return ConnectionFiles(
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        expected_host_key=expected,
        observed_host_key=observed,
        ssh_config=ssh_config,
        wrapper_dir=wrapper_dir,
    )


def receive_work_tree(test_dir: Path, staging: Path) -> Path:
    archive = test_dir / "server-source.tar"
    with archive.open("wb") as destination:
        shutil.copyfileobj(sys.stdin.buffer, destination)
    if archive.stat().st_size == 0:
        raise LiveFailure("automatic live received an empty work-tree archive")
    with tarfile.open(archive, "r") as source:
        names = set(source.getnames())
        if "remote-ssh-mcp.py" not in names or not any(
            name.startswith("remote_ssh_mcp/") for name in names
        ):
            raise LiveFailure("work-tree archive does not contain the MCP server")
        if any(
            name == ".live-server" or name.startswith(".live-server/") for name in names
        ):
            raise LiveFailure("work tree contains the reserved .live-server path")
    with tarfile.open(archive, "a") as destination:
        destination.add(staging, arcname=".live-server", recursive=True)
    return archive


def verify_server_confinement(
    resources: LiveResources, server: str, network: str
) -> None:
    status = podman_exec(
        resources,
        server,
        "cat",
        "/proc/self/status",
        purpose="reading server confinement status",
    ).decode()
    uid = (
        podman_exec(resources, server, "id", "-u", purpose="reading server uid")
        .decode()
        .strip()
    )
    if uid == "0" or "CapEff:\t0000000000000000" not in status:
        raise LiveFailure("server is root or holds effective capabilities")
    if "NoNewPrivs:\t1" not in status or "Seccomp:\t2" not in status:
        raise LiveFailure("server privilege or seccomp policy is not active")
    read_only = podman_output(
        resources,
        "inspect",
        "--format",
        "{{.HostConfig.ReadonlyRootfs}}",
        server,
        purpose="inspecting the server root filesystem",
    )
    pids_limit = podman_output(
        resources,
        "inspect",
        "--format",
        "{{.HostConfig.PidsLimit}}",
        server,
        purpose="inspecting the server process limit",
    )
    mounts_text = podman_output(
        resources,
        "inspect",
        "--format",
        "{{json .Mounts}}",
        server,
        purpose="inspecting server mounts",
    )
    mounts = json.loads(mounts_text)
    if not isinstance(mounts, list):
        raise LiveFailure("Podman returned malformed server mount metadata")
    allowed_tmpfs = {"/tmp", "/work", "/home/box"}
    for mount in mounts:
        if not isinstance(mount, dict):
            raise LiveFailure("Podman returned malformed server mount metadata")
        if (
            mount.get("Type") != "tmpfs"
            or mount.get("Destination") not in allowed_tmpfs
        ):
            raise LiveFailure("server has a bind, volume, or unexpected mount")
    attached = podman_output(
        resources,
        "inspect",
        "--format",
        "{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}",
        server,
        purpose="inspecting server networks",
    ).split()
    if read_only != "true" or pids_limit != "1024":
        raise LiveFailure("server filesystem or resource policy was not applied")
    if attached != [network]:
        raise LiveFailure("server is not isolated to the per-run network")


def provision_server(
    resources: LiveResources,
    image: str,
    policy: list[str],
    network: str,
    connection: ConnectionFiles,
    key: KeyMaterial,
    test_dir: Path,
) -> str:
    server = unique_name("remote-ssh-mcp-server")
    resources.server_name = server
    print("live: creating the confined server container", file=sys.stderr)
    checked(
        [
            resources.podman,
            "create",
            "--name",
            server,
            "--label",
            f"{RUN_LABEL}={server}",
            "--label",
            f"{OWNER_LABEL}=live-server",
            "--network",
            network,
            "--entrypoint",
            '["/usr/bin/tini","--","sleep","infinity"]',
            *policy,
            image,
        ],
        "creating the server container",
    )
    checked([resources.podman, "start", server], "starting the server container")

    staging = test_dir / "server-stage"
    ssh_staging = staging / "home/.ssh"
    local_staging = staging / "local-root/downloads"
    ssh_staging.mkdir(parents=True, mode=0o700)
    local_staging.mkdir(parents=True, mode=0o700)
    shutil.copy2(connection.ssh_config, ssh_staging / "config")
    shutil.copy2(test_dir / "known_hosts", ssh_staging / "known_hosts")
    shutil.copy2(key.identity_file, ssh_staging / "id_ed25519")
    for path in ssh_staging.iterdir():
        path.chmod(0o600)
    archive = receive_work_tree(test_dir, staging)
    prepare_script = (
        'install -d -m 0700 "$HOME/.ssh" /work/local-root '
        "/work/local-root/downloads; "
        'cp -- .live-server/home/.ssh/config "$HOME/.ssh/config"; '
        'cp -- .live-server/home/.ssh/known_hosts "$HOME/.ssh/known_hosts"; '
        'cp -- .live-server/home/.ssh/id_ed25519 "$HOME/.ssh/id_ed25519"; '
        'chmod 0600 "$HOME/.ssh/config" "$HOME/.ssh/known_hosts" '
        '"$HOME/.ssh/id_ed25519"; find .live-server -depth -delete'
    )
    with archive.open("rb") as source:
        checked(
            [
                resources.podman,
                "exec",
                "--interactive",
                server,
                "/usr/local/sbin/toolbox-entrypoint",
                "sh",
                "-ceu",
                prepare_script,
            ],
            "streaming the work tree into the server",
            stdin=source,
        )
    verify_server_confinement(resources, server, network)
    return server


def verify_ssh_settings(
    resources: LiveResources,
    connection: ConnectionFiles,
    server: str | None,
) -> tuple[str, str]:
    real_ssh = require_program("ssh")
    if server is not None:
        settings = podman_exec(
            resources,
            server,
            "ssh",
            "-G",
            TARGET_ALIAS,
            purpose="resolving server-container SSH configuration",
        ).decode()
    else:
        environment = os.environ.copy()
        environment["PATH"] = f"{connection.wrapper_dir}:{environment['PATH']}"
        environment["REMOTE_SSH_MCP_TEST_SSH_CONFIG"] = str(connection.ssh_config)
        environment["REMOTE_SSH_MCP_TEST_REAL_SSH"] = real_ssh
        completed = run_process(
            ["ssh", "-G", TARGET_ALIAS],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise LiveFailure("resolving host SSH configuration failed")
        settings = completed.stdout
    expected = (
        f"hostname {connection.ssh_host}",
        f"port {connection.ssh_port}",
        "user mcp-test",
        "stricthostkeychecking true",
        "identityagent none",
        "passwordauthentication no",
        "kbdinteractiveauthentication no",
    )
    setting_lines = set(settings.splitlines())
    for value in expected:
        if value not in setting_lines:
            raise LiveFailure(f"SSH setting is not effective: {value}")
    return real_ssh, settings


def target_log(resources: LiveResources, target: str) -> str:
    completed = run_process(
        [resources.podman, "logs", target],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise LiveFailure("reading the target log failed")
    return (completed.stdout + completed.stderr).decode(errors="replace")
