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

Requirements: Python 3.11 or newer with `venv`, GNU Make, OpenSSH, and rsync
locally; a Linux remote with `/bin/sh`, rsync, GNU coreutils/findutils,
`setsid`, and `/bin/kill`.

```bash
cd /path/to/remote-ssh-mcp
make runtime-venv
mkdir -m 700 "$HOME/remote-machine-files"
export PATH="$PWD:$PATH"
remote-ssh-mcp --help
```

**`make runtime-venv` is a required, explicit installation step before adding
the launcher to an agent configuration.** It installs the hash-verified runtime
lock into `venv-runtime/` on this host. Review that trust decision yourself.
The launcher never creates an environment, invokes pip, or updates packages; it
refuses to start when the prepared runtime is missing or stale.

After installation, add `remote-ssh-mcp` to Codex or Claude Code, start the MCP
server, then let the agent call `connect` with an SSH alias or a host/user/port
tuple.

## Documentation

- [Getting started and client setup](doc/getting-started.md)
- [MCP tools and operations](doc/tools.md)
- [Security model](doc/security.md)
- [Architecture and design decisions](doc/architecture.md)
- [Development and live container testing](doc/development.md)
- [Contributing](doc/contributing.md)
- [Complete documentation index](doc/README.md)

Ready-to-adapt client configurations are in [`doc/examples/`](doc/examples/).

## Tested End To End

The full MCP surface is covered by unit and process-level integration tests and
by automatic ephemeral-key and hardware-key runs against a disposable Debian
container with real OpenSSH, rsync, NOPASSWD sudo, large transfers, and master
loss.

Released under the [MIT License](LICENSE).
