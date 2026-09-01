"""Live-test command-line application and end-to-end orchestration."""

from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import tempfile
import time
from pathlib import Path
from types import FrameType

from ssh_wrapper.session_environment import SESSION_ENVIRONMENT_VARIABLES

from tools import container_payload

from .connection import (
    prepare_connection_files,
    prepare_host_server_repository,
    provision_server,
    target_log,
    verify_ssh_settings,
)
from .keys import preflight, prepare_key
from .process import (
    LIVE_RUNNER,
    SERVER_REPOSITORY,
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

LOG_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2} "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} ",
    re.MULTILINE,
)


def parse_arguments(argv: list[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser(
        description="Run the MCP live matrix against a disposable Podman SSH target."
    )
    parser.add_argument("--image", required=True, help="target image built by Make")
    parser.add_argument(
        "--server-image", help="toolbox image used by the automatic server"
    )
    parser.add_argument("--mode", choices=("ephemeral", "fido"), default="ephemeral")
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--identity-file", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--strip-session-environment",
        action="store_true",
        help="remove the recoverable session variables from the MCP subprocess",
    )
    parsed = parser.parse_args(argv)
    if parsed.strip_session_environment and (
        parsed.mode != "fido" or parsed.server_image is not None
    ):
        parser.error("--strip-session-environment requires the host-side MCP FIDO mode")
    return Arguments(
        image=parsed.image,
        server_image=parsed.server_image,
        mode=parsed.mode,
        public_key=parsed.public_key,
        identity_file=parsed.identity_file,
        preflight_only=parsed.preflight_only,
        strip_session_environment=parsed.strip_session_environment,
    )


def run_matrix(
    resources: LiveResources,
    target: str,
    server: str | None,
    connection: ConnectionFiles,
    test_dir: Path,
    real_ssh: str,
    *,
    strip_session_environment: bool,
) -> tuple[Path, tuple[str, ...]]:
    accepted_before = target_log(resources, target).count(
        "Accepted publickey for mcp-test"
    )
    if accepted_before != 0:
        raise LiveFailure("test target already contains a successful authentication")

    environment = os.environ.copy()
    session_values = (
        tuple(
            value
            for name in SESSION_ENVIRONMENT_VARIABLES
            if (value := environment.get(name))
        )
        if strip_session_environment
        else ()
    )
    environment.update(
        {
            "PODMAN": resources.podman,
            "REMOTE_SSH_MCP_E2E_CONTAINER": target,
            "REMOTE_SSH_MCP_E2E_TARGET": TARGET_ALIAS,
            "REMOTE_SSH_MCP_E2E_STDERR": str(test_dir / "server.stderr"),
        }
    )
    if strip_session_environment:
        environment["REMOTE_SSH_MCP_E2E_STRIP_SESSION_ENVIRONMENT"] = "1"
    if server is not None:
        stderr_path = test_dir / "server.stderr"
        environment.update(
            {
                "REMOTE_SSH_MCP_E2E_SERVER_CONTAINER": server,
                "REMOTE_SSH_MCP_E2E_REPOSITORY": str(SERVER_REPOSITORY),
            }
        )
    else:
        repository = prepare_host_server_repository(test_dir)
        stderr_path = test_dir / "server.stderr"
        environment.update(
            {
                "REMOTE_SSH_MCP_E2E_REPOSITORY": str(repository),
                "REMOTE_SSH_MCP_E2E_LAUNCHER": str(repository / "remote-ssh-mcp"),
                "REMOTE_SSH_MCP_TEST_SSH_CONFIG": str(connection.ssh_config),
                "REMOTE_SSH_MCP_E2E_WRAPPER_DIR": str(connection.wrapper_dir),
                "REMOTE_SSH_MCP_TEST_REAL_SSH": real_ssh,
            }
        )
    completed = run_process(
        [sys.executable, str(LIVE_RUNNER)], env=environment, check=False
    )
    if completed.returncode != 0:
        log_tail = "\n".join(target_log(resources, target).splitlines()[-20:])
        if log_tail:
            print(f"live: target sshd log tail:\n{log_tail}", file=sys.stderr)
        raise LiveFailure(f"live MCP matrix failed with status {completed.returncode}")
    return stderr_path, session_values


def confirm_authentication(server: str | None) -> None:
    if server is not None:
        print(
            "live: target ready; authenticating from the confined companion container.",
            file=sys.stderr,
        )
        return
    print(
        "live: target ready; the SSH master will authenticate now.\n"
        "live: confirm the system PIN dialog if shown, then touch the key when asked.",
        file=sys.stderr,
    )
    if not sys.stdin.isatty():
        raise LiveFailure("FIDO live test requires interactive stdin")
    input("live: press Enter when ready to open the SSH master: ")


def verify_key_postconditions(
    key: KeyMaterial,
    private_before: tuple[int, ...],
    public_before: str,
) -> None:
    if file_metadata(key.identity_file) != private_before:
        raise LiveFailure("identity file metadata changed during the test")
    if file_digest(key.public_key) != public_before:
        raise LiveFailure("public key changed during the test")


def verify_mcp_postconditions(
    resources: LiveResources,
    target: str,
    key: KeyMaterial,
    diagnostics: Path,
    session_values: tuple[str, ...],
) -> None:
    diagnostic_text = diagnostics.read_text(encoding="utf-8")
    diagnostic_payload = LOG_TIMESTAMP.sub("", diagnostic_text)
    if (
        str(key.identity_file) in diagnostic_text
        or str(key.public_key) in diagnostic_text
    ):
        raise LiveFailure("a supplied key path appeared in server diagnostics")
    if any(value in diagnostic_payload for value in session_values):
        raise LiveFailure(
            "a graphical or SSH-agent session value appeared in server diagnostics"
        )
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

    target_policy = parse_policy("REMOTE_SSH_MCP_LIVE_TARGET_CONFINE")
    server_policy: list[str] | None = None
    if arguments.server_image is not None:
        server_policy = parse_policy("REMOTE_SSH_MCP_LIVE_SERVER_CONFINE")

    test_directory = tempfile.TemporaryDirectory(
        prefix="remote-ssh-mcp-e2e.", dir=os.environ.get("TMPDIR")
    )
    test_dir = Path(test_directory.name)
    private_before = file_metadata(key.identity_file)
    public_before = file_digest(key.public_key)
    target_port = 22
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
            target_port,
        )
        provision_target_fixtures(resources, target)
        connection = prepare_connection_files(
            resources,
            target,
            key,
            test_dir,
            arguments.server_image is not None,
            target_port,
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
        confirm_authentication(server)
        diagnostics, session_values = run_matrix(
            resources,
            target,
            server,
            connection,
            test_dir,
            real_ssh,
            strip_session_environment=arguments.strip_session_environment,
        )
        verify_mcp_postconditions(resources, target, key, diagnostics, session_values)
        verify_key_postconditions(key, private_before, public_before)
        print(
            "live: MCP matrix complete; "
            f"host key {connection.observed_host_key}; "
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
    except (LiveFailure, OSError, container_payload.PayloadError) as error:
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
