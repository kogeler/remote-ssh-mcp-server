# Remote SSH MCP

**Give AI coding agents reliable remote-machine access without handing them a
raw terminal.**

Remote SSH MCP turns one ordinary OpenSSH connection into 13 structured tools
for Codex, Claude Code, and other MCP clients. Agents can inspect files, run
bounded commands, use passwordless sudo, and move large files without pushing
terminal noise or file contents through the model context.

## Why Use It?

- **Authenticate once.** A deliberate `connect` opens one native OpenSSH
  master; every command and transfer reuses it. FIDO2/YubiKey PIN and touch
  happen once per connection, not once per tool call.
- **Move large files properly.** Background rsync transfers support progress,
  cancellation, resume, SHA-256 verification, and atomic publication.
- **Fail closed.** A lost master never reconnects silently, sudo never asks for
  a password, and mux clients cannot fall back to a new SSH login.
- **Keep control visible.** The server starts disconnected, exposes no network
  listener, and separates read-only tools from state-changing operations for
  client approval policies.
- **Use your existing SSH setup.** Host aliases, ProxyJump, host keys, agents,
  and hardware-token integration remain OpenSSH's responsibility.

## Quick Start

Requirements: Python 3, OpenSSH, and rsync locally; a POSIX shell, rsync, and
sha256sum remotely.

```bash
cd /path/to/remote-ssh-mcp
export PATH="$PWD:$PATH"
mkdir -m 700 "$HOME/remote-machine-files"
remote-ssh-mcp --help
```

The launcher creates its own pinned virtual environment. Add it to Codex or
Claude Code, start the MCP server, then let the agent call `connect` with an SSH
alias or a host/user/port tuple.

## Documentation

- [Getting started and client setup](doc/getting-started.md)
- [MCP tools and operations](doc/tools.md)
- [Security model](doc/security.md)
- [Architecture and design decisions](doc/architecture.md)
- [Development and live LXC testing](doc/development.md)
- [Complete documentation index](doc/README.md)

Ready-to-adapt client configurations are in [`doc/examples/`](doc/examples/).

## Tested End To End

The full MCP surface is covered by unit and process-level integration tests and
by automatic ephemeral-key and hardware-key runs against disposable Debian LXC
targets with real OpenSSH, rsync, NOPASSWD sudo, large transfers, and master
loss.

Released under the [MIT License](LICENSE).
