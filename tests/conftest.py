from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from remote_ssh_mcp.config import RuntimeConfig


@pytest.fixture
def runtime_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig.from_namespace(
        argparse.Namespace(
            connect_timeout=2.0,
            command_timeout=2.0,
            max_output_bytes=64 * 1024,
            max_transfers=2,
            log_level="DEBUG",
        ),
        repository_root=tmp_path,
    )
