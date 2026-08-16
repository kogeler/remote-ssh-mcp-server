"""Live-test command-line application and end-to-end orchestration."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from types import FrameType

from .connection import (
    prepare_connection_files,
    provision_server,
    target_log,
    verify_ssh_settings,
)
from .keys import preflight, prepare_key
from .process import (
    LIVE_RUNNER,
    SERVER_LOCAL_ROOT,
    TARGET_ALIAS,
    Arguments,
    ConnectionFiles,
    KeyMaterial,
    LiveFailure,
    LiveResources,
    SignalExit,
    checked,
    file_digest,
    file_metadata,
    parse_policy,
    podman_exec,
    run_process,
)
from .topology import (
    create_internal_network,
    provision_target,
    provision_target_fixtures,
)


def parse_arguments(argv: list[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser(
        description="Run the live MCP matrix against disposable Podman containers."
    )
    parser.add_argument("--image", required=True, help="target image built by Make")
    parser.add_argument(
        "--server-image", help="toolbox image used by the automatic server"
    )
    parser.add_argument("--mode", choices=("ephemeral", "fido"), default="ephemeral")
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--identity-file", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    parsed = parser.parse_args(argv)
    return Arguments(
        image=parsed.image,
        server_image=parsed.server_image,
        mode=parsed.mode,
        public_key=parsed.public_key,
        identity_file=parsed.identity_file,
        preflight_only=parsed.preflight_only,
    )


def run_matrix(
    resources: LiveResources,
    target: str,
    server: str | None,
    connection: ConnectionFiles,
    test_dir: Path,
    real_ssh: str,
) -> Path:
    accepted_before = target_log(resources, target).count(
        "Accepted publickey for mcp-test"
    )
    if accepted_before != 0:
        raise LiveFailure("test target already contains a successful authentication")

    if server is None:
        print(
            "live: target ready; the SSH master will authenticate now.\n"
            "live: confirm the system PIN dialog if shown, then touch the key once.",
            file=sys.stderr,
        )
        if not sys.stdin.isatty():
            raise LiveFailure("FIDO live test requires interactive stdin")
        input("live: press Enter when ready to open the SSH master: ")
    else:
        print(
            "live: target ready; authenticating from the confined server container.",
            file=sys.stderr,
        )

    environment = os.environ.copy()
    environment.update(
        {
            "PODMAN": resources.podman,
            "REMOTE_SSH_MCP_E2E_CONTAINER": target,
            "REMOTE_SSH_MCP_E2E_TARGET": TARGET_ALIAS,
        }
    )
    if server is not None:
        stderr_path = test_dir / "server.stderr"
        environment.update(
            {
                "REMOTE_SSH_MCP_E2E_SERVER_CONTAINER": server,
                "REMOTE_SSH_MCP_E2E_LOCAL_ROOT": str(SERVER_LOCAL_ROOT),
                "REMOTE_SSH_MCP_E2E_STDERR": str(stderr_path),
            }
        )
    else:
        local_root = test_dir / "local-root"
        local_root.mkdir(mode=0o700)
        (local_root / "downloads").mkdir(mode=0o700)
        stderr_path = local_root / "server.stderr"
        environment.update(
            {
                "REMOTE_SSH_MCP_E2E_LOCAL_ROOT": str(local_root),
                "REMOTE_SSH_MCP_TEST_SSH_CONFIG": str(connection.ssh_config),
                "REMOTE_SSH_MCP_E2E_WRAPPER_DIR": str(connection.wrapper_dir),
                "REMOTE_SSH_MCP_TEST_REAL_SSH": real_ssh,
            }
        )
    completed = run_process(
        [sys.executable, str(LIVE_RUNNER)], env=environment, check=False
    )
    if completed.returncode != 0:
        raise LiveFailure(f"live MCP matrix failed with status {completed.returncode}")
    return stderr_path


def verify_postconditions(
    resources: LiveResources,
    target: str,
    key: KeyMaterial,
    private_before: tuple[int, ...],
    public_before: str,
    diagnostics: Path,
) -> None:
    if file_metadata(key.identity_file) != private_before:
        raise LiveFailure("identity file metadata changed during the test")
    if file_digest(key.public_key) != public_before:
        raise LiveFailure("public key changed during the test")
    diagnostic_text = diagnostics.read_text(encoding="utf-8")
    if (
        str(key.identity_file) in diagnostic_text
        or str(key.public_key) in diagnostic_text
    ):
        raise LiveFailure("a supplied key path appeared in server diagnostics")
    for _attempt in range(50):
        artifact = (
            podman_exec(
                resources,
                target,
                "bash",
                "-c",
                "find /tmp -maxdepth 1 -name 'remote-ssh-mcp.*' -print -quit",
                purpose="checking remote command cleanup",
            )
            .decode()
            .strip()
        )
        if not artifact:
            return
        time.sleep(0.1)
    raise LiveFailure("remote command runtime artifact remained after the test")


def run_live(arguments: Arguments, key: KeyMaterial, resources: LiveResources) -> None:
    if not LIVE_RUNNER.is_file():
        raise LiveFailure("live MCP runner is missing")
    if not Path(sys.executable).is_file():
        raise LiveFailure("Python runtime is missing; run make runtime-venv")
    checked(
        [resources.podman, "image", "exists", arguments.image],
        "checking the target image",
    )
    if arguments.server_image is not None:
        checked(
            [resources.podman, "image", "exists", arguments.server_image],
            "checking the server image",
        )

    target_policy = parse_policy("REMOTE_SSH_MCP_LIVE_TARGET_CONFINE", server=False)
    server_policy: list[str] | None = None
    if arguments.server_image is not None:
        server_policy = parse_policy("REMOTE_SSH_MCP_LIVE_SERVER_CONFINE", server=True)

    test_directory = tempfile.TemporaryDirectory(
        prefix="remote-ssh-mcp-e2e.", dir=os.environ.get("TMPDIR")
    )
    test_dir = Path(test_directory.name)
    private_before = file_metadata(key.identity_file)
    public_before = file_digest(key.public_key)
    try:
        network = (
            create_internal_network(resources)
            if arguments.server_image is not None
            else None
        )
        target = provision_target(
            resources,
            arguments.image,
            target_policy,
            key.public_key,
            public_before,
            network,
        )
        provision_target_fixtures(resources, target)
        connection = prepare_connection_files(
            resources,
            target,
            key,
            test_dir,
            arguments.server_image is not None,
        )
        server: str | None = None
        if arguments.server_image is not None:
            assert network is not None and server_policy is not None
            server = provision_server(
                resources,
                arguments.server_image,
                server_policy,
                network,
                connection,
                key,
                test_dir,
            )
        real_ssh, _settings = verify_ssh_settings(resources, connection, server)
        diagnostics = run_matrix(
            resources, target, server, connection, test_dir, real_ssh
        )
        verify_postconditions(
            resources,
            target,
            key,
            private_before,
            public_before,
            diagnostics,
        )
        print(
            f"live: complete; host key {connection.observed_host_key}; "
            "source key files unchanged",
            file=sys.stderr,
        )
    finally:
        clean = resources.cleanup()
        test_directory.cleanup()
        if not clean:
            raise LiveFailure("cleanup could not remove every owned Podman resource")


def install_signal_handlers() -> None:
    def stop(exit_code: int):
        def handler(_signal: int, _frame: FrameType | None) -> None:
            raise SignalExit(exit_code)

        return handler

    signal.signal(signal.SIGHUP, stop(129))
    signal.signal(signal.SIGINT, stop(130))
    signal.signal(signal.SIGTERM, stop(143))


def main(argv: list[str] | None = None) -> int:
    install_signal_handlers()
    resources = LiveResources(podman=os.environ.get("PODMAN", "podman"))
    key: KeyMaterial | None = None
    exit_code = 0
    try:
        arguments = parse_arguments(argv)
        key = prepare_key(arguments)
        preflight(arguments, key, resources)
        if not arguments.preflight_only:
            run_live(arguments, key, resources)
    except SystemExit as error:
        exit_code = int(error.code) if isinstance(error.code, int) else 1
    except SignalExit as error:
        exit_code = error.exit_code
    except (LiveFailure, OSError, tarfile.TarError) as error:
        print(f"live: {error}", file=sys.stderr)
        exit_code = 1
    finally:
        if (
            (resources.target_name or resources.server_name or resources.network_name)
            and not resources.cleanup()
            and exit_code == 0
        ):
            exit_code = 1
        if key is not None and key.temporary is not None:
            key.temporary.cleanup()
        print(f"live: cleanup complete (status {exit_code})", file=sys.stderr)
    return exit_code
