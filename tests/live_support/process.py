"""Process execution, shared types, and owned Podman resource cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

TOOL_ROOT = Path(__file__).resolve().parents[2]
LIVE_RUNNER = TOOL_ROOT / "tests/live_podman_e2e.py"
TARGET_ALIAS = "remote-ssh-mcp-podman-e2e"
SERVER_HOME = Path("/home/box")
SERVER_REPOSITORY = Path("/work/src/remote_ssh_mcp")
OWNER_LABEL = "remote-ssh-mcp.owner"
RUN_LABEL = "remote-ssh-mcp.run"
CLEANUP_ATTEMPTS = 5
CLEANUP_RETRY_DELAY = 0.25
LIVE_USER_NAMESPACE = "--userns=auto:size=2048"

ResourceState = Literal["absent", "owned", "foreign", "error"]


def run_process(command: list[str], **options: Any) -> subprocess.CompletedProcess[Any]:
    input_data = options.pop("input", None)
    capture_output = options.pop("capture_output", False)
    check = options.pop("check", False)
    if capture_output:
        if "stdout" in options or "stderr" in options:
            raise ValueError("stdout and stderr may not be used with capture_output")
        options["stdout"] = subprocess.PIPE
        options["stderr"] = subprocess.PIPE
    if input_data is not None:
        if options.get("stdin") is not None:
            raise ValueError("stdin and input may not be used together")
        options["stdin"] = subprocess.PIPE

    process = subprocess.Popen(command, **options)
    try:
        stdout, stderr = process.communicate(input_data)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise

    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check:
        completed.check_returncode()
    return completed


class LiveFailure(RuntimeError):
    """A concise operator-facing live-test failure."""


class SignalExit(Exception):
    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class Arguments:
    image: str
    server_image: str | None
    mode: str
    public_key: Path | None
    identity_file: Path | None
    preflight_only: bool
    strip_session_environment: bool


@dataclass(frozen=True, slots=True)
class KeyMaterial:
    public_key: Path
    identity_file: Path
    fingerprint: str
    temporary: tempfile.TemporaryDirectory[str] | None


@dataclass(frozen=True, slots=True)
class ConnectionFiles:
    ssh_host: str
    ssh_port: int
    expected_host_key: str
    observed_host_key: str
    ssh_config: Path
    wrapper_dir: Path


@dataclass(slots=True)
class LiveResources:
    podman: str
    target_name: str | None = None
    server_name: str | None = None
    network_name: str | None = None

    def _resource_state(
        self, kind: str, name: str, owner: str
    ) -> tuple[ResourceState, str]:
        exists = run_process(
            [self.podman, kind, "exists", name],
            text=True,
            capture_output=True,
            check=False,
        )
        if exists.returncode == 1:
            return "absent", ""
        if exists.returncode != 0:
            return "error", f"exists returned status {exists.returncode}"

        labels_path = ".Config.Labels" if kind == "container" else ".Labels"
        completed = run_process(
            [
                self.podman,
                kind,
                "inspect",
                "--format",
                f"{{{{json {labels_path}}}}}",
                name,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            return "error", f"inspect returned status {completed.returncode}"
        try:
            labels = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return "error", "inspect returned malformed labels"
        if not isinstance(labels, dict):
            return "error", "inspect returned non-object labels"
        if labels.get(RUN_LABEL) != name or labels.get(OWNER_LABEL) != owner:
            return "foreign", "labels do not match this run"
        return "owned", ""

    @staticmethod
    def _failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
        lines = completed.stderr.strip().splitlines()
        suffix = f": {lines[-1][:300]}" if lines else ""
        return f"status {completed.returncode}{suffix}"

    def _remove_owned(
        self, kind: str, name: str, owner: str, remove: list[str]
    ) -> bool:
        detail = ""
        for attempt in range(CLEANUP_ATTEMPTS):
            state, detail = self._resource_state(kind, name, owner)
            if state == "absent":
                return True
            if state == "foreign":
                print(
                    f"live: refusing to remove {name}: {detail}",
                    file=sys.stderr,
                )
                return False
            if state == "owned":
                completed = run_process(
                    [self.podman, *remove, name],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                detail = self._failure_detail(completed)
            if attempt + 1 < CLEANUP_ATTEMPTS:
                time.sleep(CLEANUP_RETRY_DELAY)

        state, final_detail = self._resource_state(kind, name, owner)
        if state == "absent":
            return True
        if state == "foreign" or final_detail:
            detail = final_detail
        print(
            f"live: could not remove owned {kind} {name}: {detail}",
            file=sys.stderr,
        )
        return False

    def _remove_container(self, name: str, owner: str) -> bool:
        return self._remove_owned(
            "container", name, owner, ["rm", "--force", "--time", "5"]
        )

    def _remove_network(self, name: str) -> bool:
        return self._remove_owned(
            "network", name, "live-network", ["network", "rm", "--force"]
        )

    def cleanup(self) -> bool:
        clean = True
        if self.server_name is not None:
            removed = self._remove_container(self.server_name, "live-server")
            clean = removed and clean
            if removed:
                self.server_name = None
        if self.target_name is not None:
            removed = self._remove_container(self.target_name, "live-target")
            clean = removed and clean
            if removed:
                self.target_name = None
        if self.network_name is not None:
            removed = self._remove_network(self.network_name)
            clean = removed and clean
            if removed:
                self.network_name = None
        return clean


def checked(
    command: list[str],
    purpose: str,
    *,
    input_data: bytes | None = None,
    stdin: object | None = None,
    text: bool = False,
) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
    completed = run_process(
        command,
        input=input_data,
        stdin=stdin,
        text=text,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        detail = stderr if isinstance(stderr, str) else stderr.decode(errors="replace")
        lines = detail.strip().splitlines()
        suffix = f": {lines[-1][:500]}" if lines else ""
        raise LiveFailure(
            f"{purpose} failed with status {completed.returncode}{suffix}"
        )
    return completed


def podman_output(resources: LiveResources, *arguments: str, purpose: str) -> str:
    completed = checked([resources.podman, *arguments], purpose, text=True)
    assert isinstance(completed.stdout, str)
    return completed.stdout.strip()


def podman_exec(
    resources: LiveResources,
    container: str,
    *arguments: str,
    purpose: str,
    input_data: bytes | None = None,
) -> bytes:
    interactive = ["--interactive"] if input_data is not None else []
    completed = checked(
        [resources.podman, "exec", *interactive, container, *arguments],
        purpose,
        input_data=input_data,
    )
    assert isinstance(completed.stdout, bytes)
    return completed.stdout


def require_program(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise LiveFailure(f"required command not found: {name}")
    return resolved


def validate_image_reference(reference: str, name: str) -> None:
    if not reference or any(character.isspace() for character in reference):
        raise LiveFailure(f"{name} reference is empty or contains whitespace")


def parse_policy(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw:
        raise LiveFailure(f"Make did not provide {name}")
    options = shlex.split(raw)
    forbidden = (
        "--entrypoint",
        "--label",
        "--name",
        "--network",
        "--privileged",
        "--user",
        "--volume",
        "-v",
    )
    for option in options:
        if any(
            option == forbidden_option or option.startswith(f"{forbidden_option}=")
            for forbidden_option in forbidden
        ):
            raise LiveFailure(f"{name} contains harness-owned option {option}")
    user_namespaces = [option for option in options if option.startswith("--userns")]
    if user_namespaces != [LIVE_USER_NAMESPACE]:
        raise LiveFailure(f"{name} must contain exactly {LIVE_USER_NAMESPACE}")
    return options


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def file_metadata(path: Path) -> tuple[int, ...]:
    metadata = path.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
    )


def key_fingerprint(identity: Path) -> str:
    completed = checked(
        [require_program("ssh-keygen"), "-lf", str(identity)],
        "reading an SSH key fingerprint",
        text=True,
    )
    assert isinstance(completed.stdout, str)
    fields = completed.stdout.split()
    if len(fields) < 2:
        raise LiveFailure("ssh-keygen returned a malformed fingerprint")
    return fields[1]
