# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""MCP compatibility adapter for the shared OpenSSH lifecycle."""

from __future__ import annotations

from pathlib import Path

from ssh_wrapper.connection import (
    SSH_ISOLATION_OPTIONS,
    ConnectionSpec,
    ConnectionState,
    SSHMasterSettings,
)
from ssh_wrapper.connection import (
    OpenSSHMaster as CoreOpenSSHMaster,
)

from .config import RuntimeConfig

__all__ = ["SSH_ISOLATION_OPTIONS", "ConnectionState", "OpenSSHMaster"]


class OpenSSHMaster(CoreOpenSSHMaster):
    """Preserve the MCP constructor while delegating transport ownership."""

    def __init__(
        self,
        config: RuntimeConfig,
        connection: ConnectionSpec,
        *,
        runtime_base: Path | None = None,
    ) -> None:
        self.config = config
        super().__init__(
            SSHMasterSettings(
                ssh_path=config.ssh_path,
                false_path=config.false_path,
                connect_timeout=config.connect_timeout,
                runtime_prefix="remote-ssh-mcp",
            ),
            connection,
            runtime_base=runtime_base,
        )
