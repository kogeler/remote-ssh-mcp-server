from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.live_support.connection import known_host_name, write_ssh_config
from tests.live_support.process import SECONDARY_TARGET_ALIAS, TARGET_ALIAS


def resolved(config: Path, alias: str) -> set[str]:
    ssh = shutil.which("ssh")
    assert ssh is not None
    completed = subprocess.run(
        [ssh, "-G", "-F", str(config), alias],
        capture_output=True,
        check=True,
        text=True,
    )
    return set(completed.stdout.splitlines())


def test_known_host_name_matches_openssh_port_notation() -> None:
    assert known_host_name("live-target", 22) == "live-target"
    assert known_host_name("127.0.0.1", 40022) == "[127.0.0.1]:40022"


@pytest.mark.parametrize(
    ("host", "secondary", "port"),
    [("127.0.0.1", "localhost", 40022), ("live-target", "live-target-secondary", 22)],
)
def test_secondary_alias_is_a_distinct_hostname_with_the_primary_host_key(
    tmp_path: Path, host: str, secondary: str, port: int
) -> None:
    config = tmp_path / "ssh_config"
    identity = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"

    write_ssh_config(
        config,
        host=host,
        secondary_host=secondary,
        port=port,
        identity=identity,
        known_hosts=known_hosts,
    )

    assert config.stat().st_mode & 0o777 == 0o600
    primary = resolved(config, TARGET_ALIAS)
    other = resolved(config, SECONDARY_TARGET_ALIAS)
    assert f"hostname {host}" in primary
    assert not any(line.startswith("hostkeyalias ") for line in primary)
    assert f"hostname {secondary}" in other
    assert f"hostkeyalias {known_host_name(host, port)}" in other
    assert "addressfamily inet" in other
    for settings in (primary, other):
        assert f"port {port}" in settings
        assert "user mcp-test" in settings
        assert "stricthostkeychecking true" in settings
        assert "identityagent none" in settings
        assert f"identityfile {identity}" in settings
