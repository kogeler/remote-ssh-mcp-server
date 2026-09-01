# Copyright (c) 2026 kogeler
# SPDX-License-Identifier: MIT

"""Stable internal errors shared by the MCP application layers."""

from ssh_wrapper.errors import SSHError as RemoteMCPError

__all__ = ["RemoteMCPError"]
