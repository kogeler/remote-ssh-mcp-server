# Getting Started

## Requirements

Local machine:

- Python 3 with `venv` support
- OpenSSH client
- rsync

Remote machine:

- POSIX shell
- rsync and sha256sum
- sudo only when `sudo_exec` is required

## Installation

Clone or copy the repository as one directory. Keep the `remote-ssh-mcp`
launcher beside `requirements.txt`, `remote-ssh-mcp.py`, and the
`remote_ssh_mcp/` package. Add the repository root to `PATH`:

```bash
export PATH="/path/to/remote-ssh-mcp:$PATH"
remote-ssh-mcp --help
```

On first use, the launcher creates `venv/` inside the repository and installs
the exact complete dependency tree from `requirements.txt`. Direct dependencies
are maintained separately in `pyproject.toml`; normal users and CI consume only
the reproducible freeze. The launcher records the successfully installed freeze
and retries a failed or interrupted installation next time.

Create a dedicated local root for uploads, downloads, and optional complete
output spools:

```bash
mkdir -m 700 "$HOME/remote-machine-files"
```

The root must be absolute, owned by the current user, and not writable by group
or other users. Every agent-selected local path is relative to this directory.

## Codex

Copy [the Codex example](examples/codex-config.toml) into a trusted project's
`.codex/config.toml`, then:

1. Replace `/absolute/path/to/allowed-local-root`.
2. Set `enabled = true` when the server should be available.
3. Keep the server name `remote_machine` if the documented tool policy should
   match without changes.

The example exposes all 13 tools and prompts for connection lifecycle,
commands, sudo, transfers, and cancellation. See the official
[Codex configuration reference](https://developers.openai.com/codex/config-reference).

## Claude Code

For a personal registration scoped to the current project:

```bash
claude mcp add --transport stdio --scope local remote_machine -- \
  remote-ssh-mcp --local-root /absolute/path/to/allowed-local-root
claude mcp get remote_machine
```

For a shared project registration, adapt
[`examples/claude-code-mcp.json`](examples/claude-code-mcp.json) as `.mcp.json`
or use the same command with `--scope project`.

Merge [`examples/claude-code-settings.json`](examples/claude-code-settings.json)
into `.claude/settings.local.json` for a private policy or
`.claude/settings.json` for a shared policy. The example allows passive tools
and asks before state-changing operations. Its permission names assume the MCP
server is named `remote_machine`.

Use `/mcp`, `claude mcp list`, or `claude mcp get remote_machine` for diagnosis.
See Claude Code's official [MCP](https://code.claude.com/docs/en/mcp) and
[permissions](https://code.claude.com/docs/en/permissions) documentation.

## Connect To A Machine

The MCP process starts disconnected. Targets never appear in client startup
configuration.

Use an alias from the standard OpenSSH configuration when it carries identity,
proxy, host-key, or hardware-token settings:

```json
{"ssh_alias":"production-app"}
```

Or provide a direct target; `port` defaults to 22:

```json
{"host":"host.example","user":"deploy","port":2222}
```

The `connect` call may open the normal system PIN dialog and request a hardware
key touch. Later operations reuse the same authenticated master. Call
`disconnect` before changing targets. A lost connection is never reopened
automatically.

## Server Options

```text
--local-root PATH          required local containment root
--connect-timeout SECONDS  SSH master startup deadline (default 120)
--command-timeout SECONDS  command deadline (default 120)
--max-output-bytes BYTES   captured bytes per stream (default 1048576)
--max-transfers COUNT      concurrent transfers (default 2)
--log-level LEVEL          DEBUG, INFO, WARNING, or ERROR
```

Allow enough client tool time for interactive hardware authentication. The
Codex example uses a 150-second tool timeout.

## Troubleshooting

- `not_connected`: call `connect` first.
- `already_connected` or `disconnect_required`: disconnect before selecting a
  new target or retrying after master loss.
- `connection_start_failed`: test ordinary OpenSSH with the same target and
  resolve host-key, token, proxy, or network errors locally.
- `connection_lost`: disconnect explicitly, then reconnect only when a new
  authentication is intended.
- `sudo_password_required`: add an appropriate NOPASSWD rule or avoid
  `sudo_exec`; cached authentication is deliberately ignored.
- Failed transfer: inspect its structured error and bounded stderr tail, fix
  permissions or space, then start the same source and destination to resume.
