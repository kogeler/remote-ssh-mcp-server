from __future__ import annotations

import io
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from tests.live_support import connection
from tests.live_support.process import LiveFailure
from tools import container_payload


def payload(tmp_path: Path, *names: str) -> io.BytesIO:
    entries: list[container_payload.PayloadEntry] = []
    for index, name in enumerate(names):
        source = tmp_path / f"source-{index}"
        source.write_bytes(name.encode())
        source.chmod(0o600)
        entries.append(container_payload.PayloadEntry(source, PurePosixPath(name)))
    stream = io.BytesIO()
    container_payload.write_archive(stream, entries)
    stream.seek(0)
    return stream


def install_input(monkeypatch: pytest.MonkeyPatch, stream: io.BytesIO) -> None:
    monkeypatch.setattr(connection.sys, "stdin", SimpleNamespace(buffer=stream))


def test_server_payload_requires_the_mcp_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    install_input(
        monkeypatch,
        payload(
            tmp_path,
            "unrelated/file.py",
        ),
    )

    with pytest.raises(LiveFailure, match="MCP server"):
        connection.receive_work_tree(run, tmp_path / "staging")


def test_server_payload_preserves_only_the_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    staging = tmp_path / "staging"
    staged_key = staging / "home/.ssh/id_ed25519"
    staged_key.parent.mkdir(parents=True)
    staged_key.write_bytes(b"key")
    staged_key.chmod(0o600)
    install_input(
        monkeypatch,
        payload(
            tmp_path,
            "remote_ssh_mcp/remote-ssh-mcp.py",
            "remote_ssh_mcp/remote_ssh_mcp/__init__.py",
        ),
    )

    archive = connection.receive_work_tree(run, staging)
    extracted = tmp_path / "extracted"
    with archive.open("rb") as source:
        container_payload.extract_archive(source, extracted)

    assert (extracted / "remote_ssh_mcp/remote-ssh-mcp.py").is_file()
    assert (
        extracted / "remote_ssh_mcp/.live-server/home/.ssh/id_ed25519"
    ).read_bytes() == b"key"
    assert {path.name for path in extracted.iterdir()} == {"remote_ssh_mcp"}
