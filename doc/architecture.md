# Architecture

## Design Goals

The server provides structured remote operations while preserving native
OpenSSH behavior for host keys, proxies, and hardware authentication. Its core
invariants are one deliberate authentication, one owned transport, bounded
outputs, resumable out-of-band file transfer, and deterministic cleanup.

## Process Model

```text
MCP client
  `- remote-ssh-mcp launcher
       `- Python STDIO MCP server
            |- zero or one foreground OpenSSH ControlMaster
            |- short-lived ssh mux clients for command channels
            `- background rsync processes using the same mux socket
```

The server does not listen on TCP or HTTP. MCP protocol messages use stdout;
diagnostics use stderr. The Python process owns every child it creates.

## Connection Lifecycle

The application begins `disconnected`. A strict `connect` request chooses an
OpenSSH alias or a direct host/user/port tuple and creates a private runtime
directory, control socket, and foreground master. Readiness requires a
successful OpenSSH control check.

Only after readiness are command, inspection, sudo, and transfer services
attached. The states are `starting`, `ready`, `lost`, `closing`, and `closed`,
with an external `disconnected` state when no master is owned. A target change
requires explicit disconnect. Master loss is observable but never causes
reconnection.

Mux clients contain neither a destination until final argv assembly nor any
usable fallback authentication. Rsync receives the identical transport through
its `-e` argument.

## Command Data Path

Commands are UTF-8 shell scripts delivered over stdin to a fixed remote
non-PTY shell. A supervisor creates a remote process group and watches channel
EOF so timeout, cancellation, or local process loss terminates remote children.
Each invocation is isolated and has a bounded deadline.

Stdout and stderr are drained concurrently to avoid pipe deadlocks. Capture is
bounded per stream; optional full spooling writes to protected local files.
Inspection is implemented as fixed remote programs whose machine-readable
output is parsed into strict models.

`sudo_exec` uses the same command runner with a fixed sudo remote program and a
random start marker. The marker distinguishes sudo refusal from a privileged
command that started successfully and later exited nonzero.

## Transfer Data Path

Each single-file transfer is an in-memory operation record with a random ID and
an asynchronous task. Rsync copies directly between local storage and the
remote host. Progress and output tails are bounded.

Partial names are deterministic over direction, connection identity, source,
and destination, enabling resume without a persistent database. Downloads use
a private local partial; uploads use a same-directory remote partial. SHA-256
verification precedes atomic final publication. Completed operation metadata
is retained for a bounded time, while transfer state itself does not survive an
MCP server restart.

## Components

- `cli.py`: startup policy and process entry behavior.
- `config.py`: immutable runtime settings and connection specifications.
- `server.py`: MCP lifecycle, tool registry, annotations, and public errors.
- `master.py`: OpenSSH master, mux transport, state, and cleanup.
- `commands.py`: supervised non-PTY execution and bounded capture.
- `inspection.py`: structured remote filesystem inspection.
- `sudo.py`: passwordless-only privilege boundary.
- `local_paths.py`: local containment and protected state.
- `transfers.py`: background rsync, resume, hashes, and publication.
- `mcp_models.py`: strict protocol input and output schemas.

## Key Decisions

- **Native OpenSSH:** preserves mature SSH config, ProxyJump, host-key checking,
  and system FIDO2/PIN behavior.
- **Local STDIO MCP:** avoids another network service and authentication layer.
- **Explicit lifecycle:** prevents a client startup or transport failure from
  unexpectedly requesting a PIN or hardware touch.
- **One master:** makes authentication reuse and ownership auditable.
- **Rsync data plane:** streams and resumes large files without MCP payloads.
- **`sudo -n -k`:** enforces cache-independent NOPASSWD behavior.
- **Explicit runtime installation:** `make runtime-venv` owns a persistent,
  runtime-only host environment; the launcher validates and uses it but never
  installs code.
- **Split development environments:** Ruff alone has a hash-verified host venv
  for editor integration, project-aware checks run in a confined toolbox, and
  host-only tests use a temporary venv removed at exit.

## Repository Layout

```text
remote-ssh-mcp              Bash runtime validator and launcher
remote-ssh-mcp.py           Python executable entry point
remote_ssh_mcp/             implementation package
tests/                      local and opt-in live tests
tests/live_harness.py       thin Podman live-test CLI
tests/live_support/         live topology, SSH material, cleanup, and orchestration
doc/                        user and maintainer documentation
requirements.txt            hashed runtime dependency lock
requirements-dev.txt        hashed runtime and development lock
requirements-lint.txt       hashed Ruff-only host-tool lock
pyproject.toml              runtime/toolbox dependencies and project metadata
tools/lint/pyproject.toml   isolated Ruff environment manifest
containers/                 toolbox/resolver and live-target definitions
make/                       shared container and live-test policy
Makefile                    development and validation interface
AGENTS.md                   agent navigation and invariants
```

## Possible Future Scope

The current boundaries are intentional, but compatible future work could add:

- recursive directory synchronization with explicit trailing-slash semantics;
- granular argv-based sudo compatible with command-specific sudoers rules;
- persistent transfer metadata and stale-partial retention policy;
- optional bandwidth limits and transfer scheduling;
- an independent remote path root below the SSH account's authority;
- tested support for non-Linux remote systems;
- optional audit or telemetry integration that preserves secret boundaries.
