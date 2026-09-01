from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from tools import container_payload


def private_file(path: Path, value: bytes = b"payload", mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(mode)
    return path


def archive_for(*entries: container_payload.PayloadEntry) -> bytes:
    stream = io.BytesIO()
    container_payload.write_archive(stream, entries)
    return stream.getvalue()


def test_archive_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    first = private_file(tmp_path / "first", b"first")
    executable = private_file(tmp_path / "executable", b"run", 0o700)
    entries = (
        container_payload.PayloadEntry(executable, PurePosixPath("bin/run")),
        container_payload.PayloadEntry(first, PurePosixPath("first")),
    )

    one = archive_for(*entries)
    two = archive_for(*reversed(entries))
    destination = tmp_path / "result"
    digests = container_payload.extract_archive(io.BytesIO(one), destination)

    assert one == two
    assert (destination / "first").read_bytes() == b"first"
    assert (destination / "bin/run").read_bytes() == b"run"
    assert (destination / "first").stat().st_mode & 0o777 == 0o600
    assert (destination / "bin/run").stat().st_mode & 0o777 == 0o700
    assert set(digests) == {"bin/run", "first"}


@pytest.mark.parametrize("name", ("", ".", "../escape", "/absolute", "a/../b"))
def test_archive_path_rejects_unsafe_value(name: str) -> None:
    with pytest.raises(container_payload.PayloadError, match="path is unsafe"):
        container_payload.safe_archive_path(name)


def test_writer_rejects_symlink_and_hard_link(tmp_path: Path) -> None:
    source = private_file(tmp_path / "source")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(source)
    hard_link = tmp_path / "hard-link"
    os.link(source, hard_link)

    for path in (symlink, source):
        with pytest.raises(container_payload.PayloadError):
            archive_for(container_payload.PayloadEntry(path, PurePosixPath("payload")))


def test_writer_rejects_duplicate_member(tmp_path: Path) -> None:
    first = private_file(tmp_path / "first")
    second = private_file(tmp_path / "second")

    with pytest.raises(container_payload.PayloadError, match="duplicate"):
        archive_for(
            container_payload.PayloadEntry(first, PurePosixPath("same")),
            container_payload.PayloadEntry(second, PurePosixPath("same")),
        )


def test_writer_enforces_member_and_byte_limits(tmp_path: Path) -> None:
    source = private_file(tmp_path / "source", b"too large")
    entry = container_payload.PayloadEntry(source, PurePosixPath("source"))

    with pytest.raises(container_payload.PayloadError, match="bytes"):
        container_payload.write_archive(io.BytesIO(), (entry,), max_bytes=1)
    with pytest.raises(container_payload.PayloadError, match="limits"):
        container_payload.write_archive(io.BytesIO(), (entry,), max_members=0)


def raw_archive(info: tarfile.TarInfo, value: bytes = b"") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        archive.addfile(info, io.BytesIO(value) if info.isreg() else None)
    return stream.getvalue()


def test_reader_rejects_path_traversal(tmp_path: Path) -> None:
    info = tarfile.TarInfo("../escape")
    info.size = 1
    info.mode = 0o600

    with pytest.raises(container_payload.PayloadError, match="path is unsafe"):
        container_payload.extract_archive(
            io.BytesIO(raw_archive(info, b"x")), tmp_path / "result"
        )
    assert not (tmp_path / "escape").exists()


def test_reader_rejects_non_regular_member(tmp_path: Path) -> None:
    info = tarfile.TarInfo("directory")
    info.type = tarfile.DIRTYPE
    info.mode = 0o700

    with pytest.raises(container_payload.PayloadError, match="metadata is invalid"):
        container_payload.extract_archive(
            io.BytesIO(raw_archive(info)), tmp_path / "result"
        )


def test_reader_rejects_noncanonical_metadata(tmp_path: Path) -> None:
    info = tarfile.TarInfo("payload")
    info.size = 1
    info.mode = 0o644

    with pytest.raises(container_payload.PayloadError, match="metadata is invalid"):
        container_payload.extract_archive(
            io.BytesIO(raw_archive(info, b"x")), tmp_path / "result"
        )


def test_reader_rejects_existing_destination(tmp_path: Path) -> None:
    source = private_file(tmp_path / "source")
    archive = archive_for(
        container_payload.PayloadEntry(source, PurePosixPath("source"))
    )
    destination = tmp_path / "result"
    destination.mkdir()

    with pytest.raises(container_payload.PayloadError, match="already exists"):
        container_payload.extract_archive(io.BytesIO(archive), destination)


def test_merge_replaces_only_regular_files(tmp_path: Path) -> None:
    source = private_file(tmp_path / "source", b"new")
    archive = archive_for(
        container_payload.PayloadEntry(source, PurePosixPath("nested/result"))
    )
    destination = tmp_path / "destination"
    private_file(destination / "nested/result", b"old")

    container_payload.merge_archive(io.BytesIO(archive), destination)

    assert (destination / "nested/result").read_bytes() == b"new"


def test_merge_stages_on_destination_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = private_file(tmp_path / "source", b"new")
    archive = archive_for(
        container_payload.PayloadEntry(source, PurePosixPath("result"))
    )
    destination = tmp_path / "destination"
    destination.mkdir()
    real_mkdtemp = container_payload.tempfile.mkdtemp
    staging_parents: list[Path] = []

    def recording_mkdtemp(*, prefix: str, dir: Path) -> str:
        staging_parents.append(Path(dir))
        return real_mkdtemp(prefix=prefix, dir=dir)

    monkeypatch.setattr(container_payload.tempfile, "mkdtemp", recording_mkdtemp)

    container_payload.merge_archive(io.BytesIO(archive), destination)

    assert staging_parents == [destination.absolute()]
    assert (destination / "result").read_bytes() == b"new"
    assert not tuple(destination.glob(".container-payload-*"))


def test_entries_from_null_are_rooted_and_normalized(tmp_path: Path) -> None:
    source = private_file(tmp_path / "nested/source")
    entries = container_payload.entries_from_null(
        io.BytesIO(b"nested/source\0"), tmp_path
    )

    assert entries == (
        container_payload.PayloadEntry(source, PurePosixPath("nested/source")),
    )


def test_mapped_entries_keep_archive_prefixes_and_separate_roots(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    support = tmp_path / "support"
    project_file = private_file(project / "pyproject.toml", b"project")
    support_file = private_file(support / "package/__init__.py", b"support")

    entries = container_payload.mapped_entries_from_null(
        io.BytesIO(b"remote_ssh_mcp/pyproject.toml\0support/package/__init__.py\0"),
        {"remote_ssh_mcp": project, "support": support},
    )

    assert entries == (
        container_payload.PayloadEntry(
            project_file, PurePosixPath("remote_ssh_mcp/pyproject.toml")
        ),
        container_payload.PayloadEntry(
            support_file,
            PurePosixPath("support/package/__init__.py"),
        ),
    )


@pytest.mark.parametrize(
    "value",
    (
        b"unmapped/file\0",
        b"remote_ssh_mcp\0",
        b"remote_ssh_mcp/../escape\0",
    ),
)
def test_mapped_entries_reject_unowned_or_unsafe_paths(
    tmp_path: Path, value: bytes
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(container_payload.PayloadError):
        container_payload.mapped_entries_from_null(
            io.BytesIO(value), {"remote_ssh_mcp": project}
        )


def test_cli_exposes_separate_mapped_payload_roots() -> None:
    arguments = container_payload.parser().parse_args(
        (
            "create",
            "--map-root=remote_ssh_mcp=.",
            "--map-root=support=../support",
            "--null-mapped-files-from-stdin",
        )
    )

    assert arguments.root is None
    assert arguments.map_root == [
        "remote_ssh_mcp=.",
        "support=../support",
    ]
    assert arguments.null_mapped_files_from_stdin is True


def test_reader_rejects_nonzero_trailer(tmp_path: Path) -> None:
    source = private_file(tmp_path / "source")
    archive = archive_for(
        container_payload.PayloadEntry(source, PurePosixPath("source"))
    )

    with pytest.raises(container_payload.PayloadError, match="trailing"):
        container_payload.extract_archive(
            io.BytesIO(archive + b"unexpected"), tmp_path / "result"
        )


def test_cli_exposes_explicit_payload_limits() -> None:
    arguments = container_payload.parser().parse_args(
        (
            "extract",
            "--destination",
            "/run/payload",
            "--max-members",
            "2",
            "--max-bytes",
            "4096",
        )
    )

    assert arguments.max_members == 2
    assert arguments.max_bytes == 4096
