#!/usr/bin/env python3

# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Create and receive bounded plain-tar payloads over container pipes."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final

DEFAULT_MAX_MEMBERS: Final = 100_000
DEFAULT_MAX_BYTES: Final = 8 * 1024 * 1024 * 1024
ARCHIVE_OVERHEAD_PER_MEMBER: Final = 8 * 1024
COPY_BLOCK_SIZE: Final = 1024 * 1024


class PayloadError(RuntimeError):
    """Raised when a container payload violates the transport contract."""


@dataclass(frozen=True, slots=True)
class PayloadEntry:
    """Map one ordinary local file to one relative archive path."""

    source: Path
    archive_path: PurePosixPath


@dataclass(frozen=True, slots=True)
class _FrozenEntry:
    source: Path
    archive_path: PurePosixPath
    details: os.stat_result


class _BoundedReader:
    def __init__(self, stream: BinaryIO, maximum: int) -> None:
        self.stream = stream
        self.maximum = maximum
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.maximum - self.count
        request = remaining + 1 if size < 0 else min(size, remaining + 1)
        value = self.stream.read(request)
        if not isinstance(value, bytes):
            raise PayloadError("payload stream did not return bytes")
        self.count += len(value)
        if self.count > self.maximum:
            raise PayloadError(f"payload archive exceeds {self.maximum} raw bytes")
        return value


def safe_archive_path(value: str | PurePosixPath) -> PurePosixPath:
    """Return one normalized, non-empty, relative payload path."""
    raw = str(value)
    path = PurePosixPath(raw)
    if (
        not raw
        or "\x00" in raw
        or path.is_absolute()
        or raw == "."
        or raw != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PayloadError(f"payload path is unsafe: {value!r}")
    return path


def _freeze_entries(
    entries: Iterable[PayloadEntry],
    *,
    max_members: int,
    max_bytes: int,
    allow_empty: bool,
) -> tuple[_FrozenEntry, ...]:
    if max_members <= 0 or max_bytes < 0:
        raise PayloadError("payload limits are invalid")
    frozen: list[_FrozenEntry] = []
    names: set[PurePosixPath] = set()
    total = 0
    for entry in entries:
        name = safe_archive_path(entry.archive_path)
        if name in names:
            raise PayloadError(f"duplicate payload member: {name}")
        try:
            details = entry.source.lstat()
        except OSError as error:
            raise PayloadError(
                f"cannot inspect payload input: {entry.source}"
            ) from error
        if not stat.S_ISREG(details.st_mode):
            raise PayloadError(f"payload input is not a regular file: {entry.source}")
        if details.st_nlink != 1:
            raise PayloadError(f"payload input has multiple links: {entry.source}")
        if len(frozen) >= max_members:
            raise PayloadError(f"payload exceeds {max_members} members")
        total += details.st_size
        if total > max_bytes:
            raise PayloadError(f"payload exceeds {max_bytes} bytes")
        names.add(name)
        frozen.append(_FrozenEntry(entry.source, name, details))
    if not frozen and not allow_empty:
        raise PayloadError("payload is empty")
    frozen.sort(key=lambda item: os.fsencode(item.archive_path.as_posix()))
    return tuple(frozen)


def _member(name: PurePosixPath, details: os.stat_result) -> tarfile.TarInfo:
    value = tarfile.TarInfo(name.as_posix())
    value.type = tarfile.REGTYPE
    value.uid = 0
    value.gid = 0
    value.uname = ""
    value.gname = ""
    value.mtime = 0
    value.mode = 0o700 if stat.S_IMODE(details.st_mode) & 0o100 else 0o600
    value.size = details.st_size
    return value


def write_archive(
    stream: BinaryIO,
    entries: Iterable[PayloadEntry],
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_empty: bool = False,
) -> None:
    """Write a deterministic archive after freezing every input identity."""
    frozen = _freeze_entries(
        entries,
        max_members=max_members,
        max_bytes=max_bytes,
        allow_empty=allow_empty,
    )
    with tarfile.open(
        fileobj=stream, mode="w|", format=tarfile.USTAR_FORMAT
    ) as archive:
        for item in frozen:
            flags = (
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = -1
            try:
                descriptor = os.open(item.source, flags)
                current = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_dev != item.details.st_dev
                    or current.st_ino != item.details.st_ino
                    or current.st_nlink != 1
                    or current.st_size != item.details.st_size
                    or current.st_mtime_ns != item.details.st_mtime_ns
                    or current.st_ctime_ns != item.details.st_ctime_ns
                ):
                    raise PayloadError(f"payload input changed: {item.source}")
                with os.fdopen(descriptor, "rb") as content:
                    descriptor = -1
                    archive.addfile(_member(item.archive_path, current), content)
            except OSError as error:
                raise PayloadError(
                    f"cannot read payload input: {item.source}"
                ) from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    for item in frozen:
        try:
            current = item.source.lstat()
        except OSError as error:
            raise PayloadError(f"payload input disappeared: {item.source}") from error
        if (
            current.st_dev != item.details.st_dev
            or current.st_ino != item.details.st_ino
            or current.st_mode != item.details.st_mode
            or current.st_nlink != 1
            or current.st_size != item.details.st_size
            or current.st_mtime_ns != item.details.st_mtime_ns
            or current.st_ctime_ns != item.details.st_ctime_ns
        ):
            raise PayloadError(f"payload input changed while streaming: {item.source}")


def _new_destination(destination: Path) -> Path:
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise PayloadError(f"payload destination already exists: {destination}")
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise PayloadError(f"payload destination parent is unsafe: {parent}")
    partial = parent / f".{destination.name}.partial"
    if partial.exists() or partial.is_symlink():
        raise PayloadError(f"payload partial already exists: {partial}")
    partial.mkdir(mode=0o700)
    return partial


def _write_member(
    archive: tarfile.TarFile,
    info: tarfile.TarInfo,
    root: Path,
) -> tuple[int, str]:
    name = safe_archive_path(info.name)
    if (
        not info.isreg()
        or info.issparse()
        or info.pax_headers
        or info.size < 0
        or info.uid != 0
        or info.gid != 0
        or info.uname
        or info.gname
        or info.mtime != 0
        or stat.S_IMODE(info.mode) not in {0o600, 0o700}
    ):
        raise PayloadError(f"payload member metadata is invalid: {name}")
    target = root.joinpath(*name.parts)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise PayloadError(f"duplicate or colliding payload member: {name}")
    source = archive.extractfile(info)
    if source is None:
        raise PayloadError(f"payload member has no content: {name}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, stat.S_IMODE(info.mode))
    size = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as output, source:
            while block := source.read(COPY_BLOCK_SIZE):
                size += len(block)
                if size > info.size:
                    raise PayloadError(f"payload member grew while reading: {name}")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size != info.size:
        target.unlink(missing_ok=True)
        raise PayloadError(f"payload member was truncated: {name}")
    return size, digest.hexdigest()


def extract_archive(
    stream: BinaryIO,
    destination: Path,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_empty: bool = False,
) -> dict[str, str]:
    """Validate and atomically extract a bounded plain archive."""
    if max_members <= 0 or max_bytes < 0:
        raise PayloadError("payload limits are invalid")
    raw_limit = max_bytes + max_members * ARCHIVE_OVERHEAD_PER_MEMBER + 10240
    reader = _BoundedReader(stream, raw_limit)
    partial = _new_destination(destination)
    digests: dict[str, str] = {}
    count = 0
    total = 0
    try:
        with tarfile.open(
            fileobj=reader, mode="r|", bufsize=tarfile.BLOCKSIZE
        ) as archive:
            for info in archive:
                count += 1
                if count > max_members:
                    raise PayloadError(f"payload exceeds {max_members} members")
                total += info.size
                if total > max_bytes:
                    raise PayloadError(f"payload exceeds {max_bytes} bytes")
                size, digest = _write_member(archive, info, partial)
                if size != info.size or info.name in digests:
                    raise PayloadError("payload member accounting is inconsistent")
                digests[info.name] = digest
        if not digests and not allow_empty:
            raise PayloadError("payload is empty")
        trailing = reader.read()
        if any(trailing):
            raise PayloadError("payload has non-zero trailing bytes")
        os.replace(partial, destination)
    except (OSError, tarfile.TarError) as error:
        raise PayloadError("payload is not a valid plain tar") from error
    finally:
        if partial.exists():
            shutil.rmtree(partial)
    return digests


def merge_archive(
    stream: BinaryIO,
    destination: Path,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_empty: bool = False,
) -> None:
    """Validate an archive fully, then replace regular files in a real tree."""
    destination = destination.absolute()
    if destination.is_symlink() or not destination.is_dir():
        raise PayloadError(f"merge destination is unsafe: {destination}")
    scratch_parent = Path(
        tempfile.mkdtemp(prefix=".container-payload-", dir=destination)
    )
    scratch = scratch_parent / "payload"
    try:
        extract_archive(
            stream,
            scratch,
            max_members=max_members,
            max_bytes=max_bytes,
            allow_empty=allow_empty,
        )
        transfers: list[tuple[Path, Path]] = []
        for source in sorted(scratch.rglob("*")):
            if source.is_dir():
                continue
            if source.is_symlink() or not source.is_file():
                raise PayloadError(f"merge payload is not regular: {source}")
            relative = source.relative_to(scratch)
            target = destination / relative
            current = destination
            for part in relative.parts[:-1]:
                current /= part
                if current.is_symlink() or (current.exists() and not current.is_dir()):
                    raise PayloadError(f"merge parent is unsafe: {current}")
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise PayloadError(f"merge target is unsafe: {target}")
            transfers.append((source, target))
        for source, target in transfers:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(source, target)
    finally:
        shutil.rmtree(scratch_parent, ignore_errors=True)


def entries_from_null(stream: BinaryIO, root: Path) -> tuple[PayloadEntry, ...]:
    """Map a NUL-delimited relative file list below one real root."""
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise PayloadError(f"payload root is unsafe: {root}")
    value = stream.read()
    if not isinstance(value, bytes):
        raise PayloadError("payload file list did not return bytes")
    entries: list[PayloadEntry] = []
    for raw in value.split(b"\0"):
        if not raw:
            continue
        try:
            text = os.fsdecode(raw)
        except UnicodeError as error:
            raise PayloadError("payload file list is not decodable") from error
        name = safe_archive_path(text)
        entries.append(PayloadEntry(root.joinpath(*name.parts), name))
    return tuple(entries)


def mapped_entries_from_null(
    stream: BinaryIO, roots: dict[str, Path]
) -> tuple[PayloadEntry, ...]:
    """Map prefixed archive names to separately rooted source trees."""
    normalized: dict[str, Path] = {}
    for prefix, root in roots.items():
        name = safe_archive_path(prefix)
        if len(name.parts) != 1 or name.as_posix() in normalized:
            raise PayloadError(f"payload mapping prefix is invalid: {prefix!r}")
        resolved = root.absolute()
        if resolved.is_symlink() or not resolved.is_dir():
            raise PayloadError(f"payload mapping root is unsafe: {root}")
        normalized[name.as_posix()] = resolved

    value = stream.read()
    if not isinstance(value, bytes):
        raise PayloadError("payload file list did not return bytes")
    entries: list[PayloadEntry] = []
    for raw in value.split(b"\0"):
        if not raw:
            continue
        try:
            archive_name = safe_archive_path(os.fsdecode(raw))
        except UnicodeError as error:
            raise PayloadError("payload file list is not decodable") from error
        prefix, *relative = archive_name.parts
        if prefix not in normalized or not relative:
            raise PayloadError(f"payload path has no mapped root: {archive_name}")
        entries.append(
            PayloadEntry(normalized[prefix].joinpath(*relative), archive_name)
        )
    return tuple(entries)


def _root_mapping(values: Sequence[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        prefix, separator, raw_root = value.partition("=")
        if not separator or not raw_root or prefix in roots:
            raise PayloadError(f"payload root mapping is invalid: {value!r}")
        roots[prefix] = Path(raw_root)
    if not roots:
        raise PayloadError("at least one payload root mapping is required")
    return roots


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--root", type=Path)
    create.add_argument("--map-root", action="append", default=[])
    create.add_argument("paths", nargs="*")
    create.add_argument("--null-files-from-stdin", action="store_true")
    create.add_argument("--null-mapped-files-from-stdin", action="store_true")
    create.add_argument("--allow-empty", action="store_true")
    extract = commands.add_parser("extract")
    extract.add_argument("--destination", type=Path, required=True)
    merge = commands.add_parser("merge")
    merge.add_argument("--destination", type=Path, required=True)
    merge.add_argument("--allow-empty", action="store_true")
    for command in (create, extract, merge):
        command.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS)
        command.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "create":
            modes = sum(
                (
                    bool(arguments.paths),
                    arguments.null_files_from_stdin,
                    arguments.null_mapped_files_from_stdin,
                )
            )
            if modes > 1:
                raise PayloadError("choose paths or one NUL file-list mode")
            if arguments.null_mapped_files_from_stdin:
                if arguments.root is not None:
                    raise PayloadError("mapped payload creation does not use --root")
                entries = mapped_entries_from_null(
                    sys.stdin.buffer, _root_mapping(arguments.map_root)
                )
            elif arguments.root is None or arguments.map_root:
                raise PayloadError("ordinary payload creation requires only --root")
            elif arguments.null_files_from_stdin:
                root = arguments.root.absolute()
                entries = entries_from_null(sys.stdin.buffer, root)
            else:
                root = arguments.root.absolute()
                entries = tuple(
                    PayloadEntry(
                        root / safe_archive_path(path), safe_archive_path(path)
                    )
                    for path in arguments.paths
                )
            write_archive(
                sys.stdout.buffer,
                entries,
                max_members=arguments.max_members,
                max_bytes=arguments.max_bytes,
                allow_empty=arguments.allow_empty,
            )
        elif arguments.command == "extract":
            extract_archive(
                sys.stdin.buffer,
                arguments.destination,
                max_members=arguments.max_members,
                max_bytes=arguments.max_bytes,
            )
        else:
            merge_archive(
                sys.stdin.buffer,
                arguments.destination,
                max_members=arguments.max_members,
                max_bytes=arguments.max_bytes,
                allow_empty=arguments.allow_empty,
            )
    except PayloadError as error:
        print(f"container payload: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
