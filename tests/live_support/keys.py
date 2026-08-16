"""SSH key preparation and host preflight checks for live runs."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from .process import (
    Arguments,
    KeyMaterial,
    LiveFailure,
    LiveResources,
    checked,
    key_fingerprint,
    podman_output,
    require_program,
    validate_image_reference,
)


def prepare_key(arguments: Arguments) -> KeyMaterial:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if arguments.mode == "ephemeral":
        if arguments.public_key is not None or arguments.identity_file is not None:
            raise LiveFailure(
                "ephemeral mode generates its own key; do not pass key paths"
            )
        temporary = tempfile.TemporaryDirectory(
            prefix="remote-ssh-mcp-live-key.", dir=os.environ.get("TMPDIR")
        )
        identity_file = Path(temporary.name) / "id_ed25519"
        checked(
            [
                require_program("ssh-keygen"),
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "remote-ssh-mcp-ephemeral-live-test",
                "-f",
                str(identity_file),
            ],
            "generating the ephemeral SSH key",
        )
        public_key = Path(f"{identity_file}.pub")
        identity_file.chmod(0o600)
        public_key.chmod(0o600)
    else:
        if arguments.server_image is not None:
            raise LiveFailure(
                "the FIDO mode needs the hardware key on this host, not in a container"
            )
        if arguments.public_key is None:
            raise LiveFailure("fido mode requires --public-key PATH")
        if arguments.identity_file is None:
            raise LiveFailure("fido mode requires --identity-file PATH")
        public_key = arguments.public_key.expanduser().resolve(strict=True)
        identity_file = arguments.identity_file.expanduser().resolve(strict=True)

    if not public_key.is_file() or not os.access(public_key, os.R_OK):
        raise LiveFailure("public key is not a readable regular file")
    if not identity_file.is_file() or not os.access(identity_file, os.R_OK):
        raise LiveFailure("identity is not a readable regular file")
    if stat.S_IMODE(identity_file.stat().st_mode) & 0o077:
        raise LiveFailure("identity file must not be accessible by group or others")

    fields = public_key.read_text(encoding="utf-8").split()
    if len(fields) < 2:
        raise LiveFailure("public key is malformed")
    key_type = fields[0]
    fido_types = {"sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"}
    if arguments.mode == "fido" and key_type not in fido_types:
        raise LiveFailure("public key is not a supported OpenSSH FIDO2 key")
    if arguments.mode == "ephemeral" and key_type != "ssh-ed25519":
        raise LiveFailure("ephemeral mode requires a standard Ed25519 key")

    return KeyMaterial(
        public_key=public_key,
        identity_file=identity_file,
        fingerprint=key_fingerprint(public_key),
        temporary=temporary,
    )


def preflight(arguments: Arguments, key: KeyMaterial, resources: LiveResources) -> None:
    validate_image_reference(arguments.image, "target image")
    if arguments.server_image is not None:
        validate_image_reference(arguments.server_image, "server image")
    require_program("ssh")
    require_program("ssh-keyscan")
    podman_output(resources, "info", purpose="querying Podman")
    rootless = podman_output(
        resources,
        "info",
        "--format",
        "{{.Host.Security.Rootless}}",
        purpose="querying Podman isolation",
    )
    if rootless != "true":
        raise LiveFailure(f"rootless Podman is required, got rootless={rootless}")
    key_type = key.public_key.read_text(encoding="utf-8").split()[0]
    print(
        f"live: preflight passed (mode {arguments.mode}, key {key_type}, "
        f"fingerprint {key.fingerprint})",
        file=sys.stderr,
    )
