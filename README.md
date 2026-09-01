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

## Requirements

- Linux on the local and remote machines
- OpenSSH and rsync on both machines, plus the standard `false` utility locally
- CPython 3.13 or 3.14 with `venv`, Bash, and GNU Make when installing from
  source; the standalone Linux executable bundles its Python runtime
- access to the published
  [`ssh-wrapper==0.1.0`](https://pypi.org/project/ssh-wrapper/0.1.0/) wheel when
  preparing a source environment
- rootless Podman only for disposable container and live acceptance targets

## Quick Start

GitHub Releases provide `remote-ssh-mcp-linux-amd64` and
`remote-ssh-mcp-linux-arm64` executables built and smoke-tested natively on
Ubuntu 26.04 runners, plus `SHA256SUMS.txt`. Download the matching binary and
checksum file, verify it, make it executable, and place it in a dedicated
writable directory on `PATH`. Relative uploads, downloads, and private runtime
state are contained by that executable's directory.

To run from a source checkout instead:

```bash
cd /path/to/remote-ssh-mcp-server
make runtime-venv
export PATH="$PWD:$PATH"
remote-ssh-mcp --help
```

`make runtime-venv` is a required, explicit installation step. It installs all
third-party dependencies as hash-verified binary packages, installs this
checkout without dependencies into `venv-runtime/`, verifies the exact
published `ssh-wrapper` dependency, and removes pip from the completed runtime.
The launcher never installs or updates packages and refuses a missing or stale
environment. Its state is bound to both `requirements.txt` and the project
`.version`, so a dependency or product-version change requires an explicit
`make runtime-venv` refresh.

After setup, add `remote-ssh-mcp` to your MCP client. The server starts
disconnected; an approved `connect` call selects an OpenSSH alias or a direct
host/user pair with an optional port.

## Documentation

- [Getting started and client setup](doc/getting-started.md)
- [MCP tools and operations](doc/tools.md)
- [Security model](doc/security.md)
- [Architecture and design decisions](doc/architecture.md)
- [Development and live container testing](doc/development.md)
- [Contributing](doc/contributing.md)
- [Complete documentation index](doc/README.md)

The same documentation is published as a browsable
[GitHub Pages site](https://kogeler.github.io/remote-ssh-mcp-server/).

Ready-to-adapt client configurations are in [`doc/examples/`](doc/examples/).

## Tested End To End

The MCP surface is covered by unit and process-level integration tests and by
an automatic ephemeral-key Podman matrix with real OpenSSH, rsync, NOPASSWD
sudo, large transfers, transport loss, and selective cleanup. Separate
operator-controlled targets exercise hardware-backed OpenSSH identities.

Released under the [MIT License](LICENSE).
