# Getting Started

## Requirements

The local machine needs Linux, OpenSSH, rsync, and the standard `false`
utility. A source installation additionally needs CPython 3.13 or 3.14 with
`venv`, Bash, and GNU Make. The remote account needs Linux, `/bin/sh`, rsync,
GNU coreutils and findutils, `setsid`, and `/bin/kill`. The optional
`sudo_exec` tool additionally requires sudo and `/bin/bash`.

### Standalone Linux executable

Each GitHub Release contains `remote-ssh-mcp-linux-amd64` and
`remote-ssh-mcp-linux-arm64`, built and smoke-tested natively on Ubuntu 26.04
runners, plus `SHA256SUMS.txt`. Download all three files, run `sha256sum
--check --ignore-missing SHA256SUMS.txt`, and install only the binary matching
the local architecture on a compatible Linux system. Keep it in a dedicated
writable directory: that directory is the standalone server's local file
boundary and owns its private `.remote-ssh-mcp/` state. The binary still
invokes the host's OpenSSH and rsync programs; it does not bundle or replace
them.

### Source checkout

Clone the repository, enter its root, prepare the explicit runtime, and check
the launcher:

```bash
cd /path/to/remote-ssh-mcp-server
make runtime-venv
export PATH="$PWD:$PATH"
remote-ssh-mcp --help
```

`make runtime-venv` resolves the exact published SSH library from PyPI and
installs every third-party dependency as a hash-locked binary package. It then
installs this checkout without dependencies. The target records the installed
lock beside `venv-runtime/`, verifies package metadata with `pip check`, and
removes pip from the completed runtime. Development tools are not installed
there.

The launcher does not run pip and does not repair a missing or stale
environment. It verifies the recorded runtime lock, the recorded project
version, the installed MCP distribution, and the installed SSH distribution,
then executes the installed MCP module with
`venv-runtime/bin/python -I -m remote_ssh_mcp`. It imports the installed SSH
distribution; the caller's current directory and inherited `PYTHONPATH` do not
select application code. Both the validation probe and server entry point use
Python isolated mode.

For a source checkout, the active virtual environment must be a direct child
of this project, and the project must contain its launcher, version, and
runtime-lock files. The server uses that verified venv owner as the local file
boundary. It refuses a global Python environment or a venv detached from the
project instead of deriving a boundary from the installed package under
`site-packages`. It also rejects a project `.version` that does not match the
active MCP package.

## MCP Client Setup

For Codex, copy the example into a trusted project's `.codex/config.toml`,
ensure its command resolves to this project's launcher, and enable the server:

- [Codex](examples/codex-config.toml)

Keep the server name `remote_machine` if you want the supplied tool approval
policy to match unchanged. Its 180-second startup deadline applies only to MCP
process initialization, which performs no SSH authentication. The separate
150-second tool deadline covers `connect`; the server itself limits master
startup to 120 seconds by default.

For a personal Claude Code registration scoped to the current project:

```bash
claude mcp add --transport stdio --scope local remote_machine -- \
  remote-ssh-mcp
claude mcp get remote_machine
```

For a shared registration, adapt the [server example](examples/claude-code-mcp.json)
as `.mcp.json` or use the same command with `--scope project`. Merge the
[permissions example](examples/claude-code-settings.json) into the appropriate
Claude settings file. Its names assume the server remains `remote_machine`.

The server starts disconnected. Connect with a trusted OpenSSH alias:

```json
{"ssh_alias":"production-app"}
```

or with a direct authority:

```json
{"host":"host.example","user":"deploy","port":2222}
```

`port` is optional in direct mode and defaults to `22`.

Only `connect` may authenticate. Call `disconnect` before changing authority
or deliberately reconnecting after transport loss.

Some stdio clients remove graphical-session and SSH-agent variables. On Linux,
the connection library may query logind for the current UID's runtime directory
and read the active user systemd manager environment. Recovery is restricted to
`DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`, `XDG_RUNTIME_DIR`,
`DBUS_SESSION_BUS_ADDRESS`, `SSH_AUTH_SOCK`, `SSH_ASKPASS`, and
`SSH_ASKPASS_REQUIRE`; an existing non-empty value always wins. The recovered
mapping belongs only to the initial OpenSSH master subprocess. It is never
passed to commands, transfers, or remote payloads. Unsupported platforms,
missing systemd tools, invalid runtime state, or an unavailable user manager
make recovery a safe no-op.

PIN and passphrase entry remains entirely in native OpenSSH and its normal
system prompt. The MCP protocol accepts no credential or PIN.

## Options

```text
-h, --help                 show command help and exit
--version                  show the installed server version and exit
--connect-timeout SECONDS  SSH master deadline, 0.1..900 (default 120)
--command-timeout SECONDS  command deadline, 0.1..86400 (default 120)
--max-output-bytes BYTES   per-stream capture, 1024..67108864 (default 1048576)
--max-transfers COUNT      concurrent transfers, 1..16 (default 2)
--log-level LEVEL          DEBUG, INFO, WARNING, or ERROR (default INFO)
```

The Codex example's 150-second per-tool deadline is longer than both server
defaults. If you deliberately raise an individual command timeout above that
value, raise the client's tool deadline as well or the client will cancel the
call first.

Local upload, download, and spool paths are relative to the selected local
root, not the caller's current working directory. That root is the source
project for the launcher and the executable's directory for a standalone
binary. See [MCP tools](tools.md) for the
complete operation contract and [Security](security.md) before granting write
or sudo approval.

## Troubleshooting

- `not_connected`: call `connect` first.
- `already_connected` or `disconnect_required`: disconnect before choosing a
  different target or retrying after master loss.
- `connection_start_failed`: run ordinary OpenSSH with the same target and
  resolve host-key, token, proxy, or network errors locally.
- An error saying no usable interactive system prompt is available means the
  bounded OpenSSH diagnostic matched a missing askpass route and session
  recovery did not find one. Start a normal graphical user session and import
  its environment into the user systemd manager; do not copy display or socket
  paths into shared MCP configuration.
- `connection_lost`: disconnect, then reconnect only when another
  authentication is intended.
- `sudo_password_required`: add an appropriately narrow NOPASSWD rule or avoid
  `sudo_exec`; cached authentication is deliberately ignored.
- After a failed transfer, inspect the structured error and bounded stderr
  tail, fix permissions or free space, and start the same source/destination
  pair to resume its deterministic partial.
