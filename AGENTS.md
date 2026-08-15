# Remote SSH MCP Agent Guide

This is the single entry point for agents changing or operating this
repository. Read the relevant documentation below before editing behavior.

## Documentation Map

- `README.md` is the concise public-facing repository overview.
- `LICENSE` contains the MIT terms for the repository.
- `pyproject.toml` is the source of direct runtime and development dependencies.
- `requirements.txt` is the generated complete dependency freeze used by the
  launcher and CI.
- `doc/getting-started.md` covers installation, Codex and Claude Code setup,
  connection modes, options, and troubleshooting.
- `doc/tools.md` defines the MCP tools, command and transfer behavior, and
  public error contract.
- `doc/security.md` defines trust boundaries, SSH isolation, local containment,
  sudo behavior, cleanup, and limitations.
- `doc/architecture.md` explains processes, lifecycle, data paths, components,
  design decisions, repository layout, and future scope.
- `doc/development.md` defines local checks, dependency updates, and the
  automatic and FIDO-assisted live LXC workflows.
- `doc/examples/` contains complete Codex and Claude Code client examples.
- `Makefile` is the supported development and validation interface.
- `.github/workflows/ci.yml` is the SHA-pinned pull-request and `main` CI,
  including CodeQL and the automatic live LXC job.
- `.github/dependabot.yml` schedules grouped Python and GitHub Actions updates.
- `.github/CODEOWNERS` assigns `@kogeler` as the default owner of every path.

Keep documentation and examples synchronized with CLI options, MCP schemas,
annotations, and security behavior. Keep detailed material out of the root
README.

## Code Map

- `remote-ssh-mcp` is the standalone PATH launcher and venv bootstrapper.
- `remote-ssh-mcp.py` is the Python executable entry point.
- `remote_ssh_mcp/cli.py` parses immutable startup policy. SSH targets must not
  become startup arguments.
- `remote_ssh_mcp/config.py` validates startup limits and connection modes.
- `remote_ssh_mcp/server.py` owns the MCP lifecycle, public schemas, safe error
  boundary, and tool registry.
- `remote_ssh_mcp/master.py` owns the single OpenSSH ControlMaster and mux-only
  client arguments.
- `remote_ssh_mcp/commands.py` executes bounded non-PTY commands and supervises
  remote cleanup.
- `remote_ssh_mcp/sudo.py` enforces cache-independent, passwordless-only sudo.
- `remote_ssh_mcp/inspection.py` implements structured remote filesystem reads.
- `remote_ssh_mcp/local_paths.py` confines local paths and private artifacts.
- `remote_ssh_mcp/transfers.py` owns resumable rsync operations, verification,
  cancellation, and atomic publication.
- `remote_ssh_mcp/mcp_models.py` contains strict protocol models.
- `tests/` contains unit, fake-process STDIO integration, launcher, and opt-in
  live LXC coverage.
- `tests/run-live-lxc.sh` generates a temporary Ed25519 key and starts the
  unattended live workflow.
- `tests/run-live-fido-lxc.sh` accepts runtime hardware-key paths and starts the
  operator-assisted live workflow.
- `tests/run-live-lxc-core.sh` provisions and removes the common disposable
  LXC target; `tests/live_lxc_e2e.py` drives the shared MCP test matrix.

## Required Invariants

- Starting the MCP server performs no SSH or network authentication.
- Only `connect` selects authority. It accepts either one trusted OpenSSH alias
  or a direct host/user/port tuple.
- One server process owns at most one SSH master. Target changes require
  `disconnect`; master loss never triggers authentication or reconnection.
- Every command and rsync process uses the existing mux socket and cannot fall
  back to a new connection. Forwarding, agent sharing, X11, and SSH-configured
  local or remote commands remain disabled.
- Standard OpenSSH configuration is trusted input. Preserve host-key checking,
  hardware-token UI, and valid proxy configurations.
- `sudo_exec` always uses `sudo -n -k`; never accept, request, cache, log, or
  transmit a password or PIN.
- Agent-provided local paths remain relative to an owned, non-shared
  `--local-root`. Protect the internal directory and reject escapes.
- Command capture stays bounded. Binary data is base64 encoded; full spooling
  happens only when explicitly requested.
- Large files use background rsync with deterministic resumable partials,
  SHA-256 verification, and atomic final publication.
- Public MCP errors must not expose internal paths, subprocess arguments,
  secrets, or diagnostics intended only for local logs.
- Disconnect, timeout, cancellation, client EOF, SIGINT, and SIGTERM clean up
  owned local and remote work without touching unrelated processes.
- Runtime code, tests, Make targets, and documentation must work with this
  directory as the repository root and must not depend on a parent checkout.

## Development Workflow

```bash
make venv
make format
make check
make ci
```

After intentionally changing dependencies, update only direct versions in
`pyproject.toml`, run `make refresh-dependencies`, and review the complete
generated tree in `requirements.txt`. Never hand-edit the freeze. Keep pytest
parallel-safe because the default suite runs through pytest-xdist workers.

Local tests must not use a real SSH identity. Keep process fakes strict enough
to prove argument vectors, one-authentication behavior, cleanup, and no-fallback
semantics.

`make live-test` is unattended and uses a newly generated ephemeral key.
`make live-fido-test` may invoke a hardware-token PIN dialog and requires the
operator checkpoint. Both create and delete an LXC instance, so run them only
on an explicitly authorized LXC test host. Pass FIDO key paths at runtime;
never embed them in source, documentation, fixtures, logs, or Git. See
`doc/development.md` for the complete procedure and cleanup contract.
