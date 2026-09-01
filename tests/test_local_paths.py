from __future__ import annotations

import os
from pathlib import Path

import pytest

from remote_ssh_mcp.errors import RemoteMCPError
from remote_ssh_mcp.local_paths import INTERNAL_DIRECTORY, LocalPathPolicy


@pytest.fixture
def policy(tmp_path: Path) -> LocalPathPolicy:
    result = LocalPathPolicy(tmp_path)
    result.initialize()
    return result


def test_internal_directories_are_private(policy: LocalPathPolicy) -> None:
    assert policy.internal_root.name == INTERNAL_DIRECTORY
    assert policy.internal_root.stat().st_mode & 0o777 == 0o700
    assert (policy.internal_root / "spool").stat().st_mode & 0o777 == 0o700


def test_internal_directories_remain_private_below_shared_root(tmp_path: Path) -> None:
    tmp_path.chmod(0o777)

    policy = LocalPathPolicy(tmp_path)
    policy.initialize()

    assert tmp_path.stat().st_mode & 0o777 == 0o777
    assert policy.internal_root.stat().st_mode & 0o777 == 0o700


def test_internal_directory_cannot_be_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-internal-outside"
    outside.mkdir()
    (tmp_path / INTERNAL_DIRECTORY).symlink_to(outside, target_is_directory=True)

    with pytest.raises(RemoteMCPError, match="real directory"):
        LocalPathPolicy(tmp_path).initialize()


def test_existing_file_resolves_inside_root(policy: LocalPathPolicy) -> None:
    expected = policy.repository / "nested" / "file.txt"
    expected.parent.mkdir()
    expected.write_text("data", encoding="utf-8")

    assert policy.resolve_existing("nested/file.txt", require_file=True) == expected


@pytest.mark.parametrize(
    "value", ["", ".", "../outside", "nested/../../outside", "/etc/passwd", "a\x00b"]
)
def test_invalid_relative_paths_are_rejected(
    policy: LocalPathPolicy, value: str
) -> None:
    with pytest.raises(RemoteMCPError) as raised:
        policy.resolve_existing(value)

    assert raised.value.code == "invalid_local_path"


def test_internal_directory_is_not_model_addressable(policy: LocalPathPolicy) -> None:
    with pytest.raises(RemoteMCPError, match="protected"):
        policy.resolve_existing(f"{INTERNAL_DIRECTORY}/spool")


def test_symlink_escape_is_rejected(policy: LocalPathPolicy, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret").write_text("secret", encoding="utf-8")
    (policy.repository / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RemoteMCPError, match="escapes"):
        policy.resolve_existing("escape/secret")


def test_destination_requires_existing_parent(policy: LocalPathPolicy) -> None:
    with pytest.raises(RemoteMCPError, match="parent"):
        policy.resolve_destination("missing/file")


def test_destination_refuses_overwrite(policy: LocalPathPolicy) -> None:
    destination = policy.repository / "result.bin"
    destination.write_bytes(b"old")

    with pytest.raises(RemoteMCPError) as raised:
        policy.resolve_destination("result.bin")
    assert raised.value.code == "local_path_exists"
    assert policy.resolve_destination("result.bin", overwrite=True) == destination


def test_destination_rejects_symlink_even_when_target_is_inside(
    policy: LocalPathPolicy,
) -> None:
    target = policy.repository / "target"
    target.write_text("data", encoding="utf-8")
    link = policy.repository / "link"
    link.symlink_to(target)

    with pytest.raises(RemoteMCPError, match="symbolic link"):
        policy.resolve_destination("link", overwrite=True)


def test_spool_paths_are_unique_and_contained(policy: LocalPathPolicy) -> None:
    first = policy.new_spool_path("stdout")
    second = policy.new_spool_path("stdout")

    assert first != second
    assert first.parent == policy.internal_root / "spool"
    assert os.path.commonpath((policy.repository, first)) == str(policy.repository)
