# Architecture

## Design Goals

The server provides structured remote operations while preserving native
OpenSSH behavior for host keys, proxies, and hardware authentication. Its core
invariants are one deliberate authentication, one owned transport, bounded
outputs, resumable out-of-band transfer, and deterministic selective cleanup.

## Process Model

```text
MCP client
  `- remote-ssh-mcp (source launcher or native standalone executable)
       `- Python STDIO server
            |- zero or one OpenSSH master from the installed ssh_wrapper wheel
            |- short-lived mux-only command channels
            `- background rsync processes over the same mux socket
```

The server opens no MCP network listener. Protocol messages use stdout and
local diagnostics use stderr. The Python process owns every child it creates.

## Boundaries

The published [`ssh-wrapper`](https://pypi.org/project/ssh-wrapper/0.1.0/)
package owns connection authority, OpenSSH process and socket ownership,
session-environment recovery, and bounded master diagnostics. This project
adapts that public library API to MCP, supervises its MCP command process
groups, and contains no second SSH transport implementation.

`remote_ssh_mcp.config` owns MCP-specific limits and local-root selection;
`remote_ssh_mcp.local_paths` owns containment below that root.
`remote_ssh_mcp.master` is the small constructor adapter. `commands`,
`inspection`, `sudo`, and `transfers` implement the MCP operations.
`mcp_models` and `server` own strict public schemas, annotations, lifecycle,
and the safe error boundary.

## Lifecycle

The process begins disconnected. `connect` selects an alias or direct
host/user pair with an optional port and starts one master. Operational
services become available only after the master is ready. A target change
requires explicit disconnect. A lost master stays lost until the caller
deliberately disconnects and reconnects.

Immediately before that first master starts, Linux may run two bounded probes.
`loginctl show-user` supplies the current UID's runtime path; only an absolute,
existing directory owned by that UID is accepted. A private probe environment
then routes `systemctl --user show-environment` to that runtime's bus. Parsing
accepts only the documented display, runtime, D-Bus, askpass, and agent
variables and fills only absent or empty inherited values. Both probes use
fixed argv, null stdin, bounded output, short deadlines, no shell, and owned
process-group cleanup. Their absence or failure leaves the inherited OpenSSH
environment unchanged.

Readiness requires a successful OpenSSH control check. States progress through
`starting`, `ready`, `lost`, `closing`, and `closed`; the MCP adapter exposes
`disconnected` when no master is owned. Only after readiness are command,
inspection, sudo, and transfer services attached. The recovered environment is
given only to the initial master. Mux channels and rsync receive neither that
mapping nor raw master diagnostics.

## Command Data Path

UTF-8 scripts enter a fixed remote non-PTY shell through stdin. A supervisor
creates one remote process group and registers a detached channel-loss watcher
before releasing the script payload through private FIFOs. The supervisor
waits on only the exec'd command process; the watcher PID and activation gate
never compete with payload input or the shell's child-status handling. Timeout,
cancellation, or local transport loss therefore terminates remote descendants
without losing a short-lived command's exit status. Stdout and stderr are
drained concurrently; each capture is bounded independently. Optional full
spooling writes only below the protected local boundary.

Inspection invokes fixed remote programs and parses their machine-readable
output into strict models. `sudo_exec` uses the same runner with a fixed sudo
program and a random start marker, distinguishing refusal before execution
from a privileged command that starts and then exits nonzero.

## Transfer Data Path

Each single-file transfer has an in-memory operation record, random public ID,
and asynchronous task. Rsync copies directly between local storage and the
remote account, while MCP carries only bounded progress and diagnostic tails.

Partial names are deterministic over direction, connection identity, source,
and destination. Downloads use a protected local partial; uploads use a
same-directory remote partial. SHA-256 verification precedes atomic final
publication. Completed metadata has bounded retention; transfer state does not
survive server restart.

## Components

- `remote_ssh_mcp/cli.py` owns command-line parsing and entry behavior.
- `remote_ssh_mcp/config.py` validates MCP limits and selects the local root.
- `remote_ssh_mcp/server.py` owns lifecycle, tool registration, annotations,
  strict schemas, and the public error boundary.
- `remote_ssh_mcp/master.py` preserves the MCP constructor while delegating the
  owned transport to `ssh_wrapper`.
- `remote_ssh_mcp/commands.py` provides supervised non-PTY execution and
  bounded capture.
- `remote_ssh_mcp/inspection.py` parses structured filesystem inspection.
- `remote_ssh_mcp/sudo.py` enforces passwordless, cache-independent sudo.
- `remote_ssh_mcp/local_paths.py` confines local paths and private state.
- `remote_ssh_mcp/transfers.py` owns rsync, resume, verification, and
  publication.
- `remote_ssh_mcp/mcp_models.py` defines strict protocol input/output models.

## Key Decisions

- Native OpenSSH preserves mature SSH configuration, ProxyJump, host-key
  checking, and system hardware-token behavior.
- Narrow Linux session recovery reaches an existing native askpass route
  without importing a complete login environment.
- Local STDIO MCP avoids another listener and authentication layer.
- Explicit connect/disconnect prevents startup or transport loss from
  unexpectedly requesting another PIN or hardware touch.
- One master makes authentication reuse and ownership auditable.
- Rsync streams and resumes large files without carrying their bytes in MCP.
- `sudo -n -k` requires an explicit NOPASSWD policy on every invocation.
- Explicit runtime installation keeps dependency mutation out of launch.
- The verified parent of the active project-owned venv is the only implicit
  local filesystem boundary; its project version must match the active package,
  and package installation paths are never boundaries.
- A standalone executable instead receives its own containing directory as an
  explicit local boundary; PyInstaller's temporary extraction directory and
  the caller's current directory are never used.
- Validated tar pipes isolate container inputs and outputs from host paths.

## Repository Layout

```text
remote-ssh-mcp             isolated runtime validator and production launcher
remote-ssh-mcp.py          explicit-root entry point for streamed live payloads
remote_ssh_mcp/            implementation package
tests/                     MCP-only unit and process tests
tests/live_support/        MCP-only live Podman orchestration
containers/                toolbox, compatibility, and disposable SSH images
make/                      container and live target definitions
tools/container_payload.py bounded deterministic pipe transport
tools/*standalone*.py      native Linux build, provenance, verification, and smoke
doc/                       project documentation and client examples
pyproject.toml             package and validation policy
requirements*.in           exact direct inputs managed by Dependabot
requirements*.txt          generated runtime, development, lint, standalone, and docs locks
Makefile                   supported local commands
```

## Possible Future Scope

Compatible additions could include recursive synchronization with explicit
trailing-slash semantics, command-specific argv sudo, persistent transfer
metadata and stale-partial policy, optional bandwidth scheduling, an
independent remote path root, non-Linux remote support, or audit integration
that preserves the existing secret and error boundaries.
