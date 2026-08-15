from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def read_record(path: Path) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    arguments: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", maxsplit=1)
        if key == "argument":
            arguments.append(value)
        else:
            fields[key] = value
    return fields, arguments


def test_automatic_live_wrapper_generates_unique_keys_and_removes_them(
    tmp_path: Path,
) -> None:
    harness_dir = tmp_path / "harness"
    key_parent = tmp_path / "keys"
    harness_dir.mkdir()
    key_parent.mkdir()
    wrapper = harness_dir / "run-live-lxc.sh"
    shutil.copy2(ROOT / "tests/run-live-lxc.sh", wrapper)
    make_executable(
        harness_dir / "run-live-lxc-core.sh",
        """#!/usr/bin/env bash
set -euo pipefail
: "${FAKE_CORE_LOG:?}"
arguments=("$@")
mode=
public_key=
identity_file=
while (( $# )); do
    case "$1" in
        --key-mode) mode=$2; shift 2 ;;
        --public-key) public_key=$2; shift 2 ;;
        --identity-file) identity_file=$2; shift 2 ;;
        *) shift ;;
    esac
done
[[ "$mode" == ephemeral ]]
[[ -f "$public_key" && -f "$identity_file" ]]
{
    printf 'mode=%s\n' "$mode"
    printf 'public_key=%s\n' "$public_key"
    printf 'identity_file=%s\n' "$identity_file"
    printf 'key_type=%s\n' "$(awk 'NR == 1 { print $1 }' "$public_key")"
    printf 'fingerprint=%s\n' "$(ssh-keygen -lf "$public_key" | awk '{ print $2 }')"
    printf 'public_mode=%s\n' "$(stat -c %a "$public_key")"
    printf 'identity_mode=%s\n' "$(stat -c %a "$identity_file")"
    printf 'argument=%s\n' "${arguments[@]}"
} > "$FAKE_CORE_LOG"
""",
    )

    records: list[dict[str, str]] = []
    for attempt in range(2):
        log = tmp_path / f"core-{attempt}.log"
        environment = os.environ.copy()
        environment.update(FAKE_CORE_LOG=str(log), TMPDIR=str(key_parent))
        completed = subprocess.run(
            [str(wrapper), "--preflight-only", "--image", "test-image"],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        record, arguments = read_record(log)
        assert record["mode"] == "ephemeral"
        assert record["key_type"] == "ssh-ed25519"
        assert record["public_mode"] == "600"
        assert record["identity_mode"] == "600"
        assert not Path(record["public_key"]).exists()
        assert not Path(record["identity_file"]).exists()
        assert arguments[-3:] == ["--image", "test-image", "--preflight-only"]
        records.append(record)

    assert records[0]["fingerprint"] != records[1]["fingerprint"]
    assert not any(key_parent.iterdir())


def test_fido_live_wrapper_selects_fido_mode_and_forwards_allowed_arguments(
    tmp_path: Path,
) -> None:
    wrapper = tmp_path / "run-live-fido-lxc.sh"
    shutil.copy2(ROOT / "tests/run-live-fido-lxc.sh", wrapper)
    make_executable(
        tmp_path / "run-live-lxc-core.sh",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" > "${FAKE_CORE_LOG:?}"
""",
    )
    log = tmp_path / "core.log"
    environment = os.environ.copy()
    environment["FAKE_CORE_LOG"] = str(log)

    completed = subprocess.run(
        [
            str(wrapper),
            "--public-key",
            "/runtime/key.pub",
            "--identity-file",
            "/runtime/key",
            "--image",
            "test-image",
            "--preflight-only",
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "--key-mode",
        "fido",
        "--public-key",
        "/runtime/key.pub",
        "--identity-file",
        "/runtime/key",
        "--image",
        "test-image",
        "--preflight-only",
    ]


def test_live_wrappers_reject_key_mode_injection() -> None:
    attempts = (
        (ROOT / "tests/run-live-lxc.sh", "--identity-file"),
        (ROOT / "tests/run-live-fido-lxc.sh", "--key-mode"),
    )

    for wrapper, option in attempts:
        completed = subprocess.run(
            [str(wrapper), option, "ephemeral"],
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode != 0
        assert f"unknown argument: {option}" in completed.stderr
