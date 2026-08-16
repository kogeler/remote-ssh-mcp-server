# Remote SSH MCP Agent Guide

This is the single entry point for agents changing or operating this
repository. Read the relevant documentation below before editing behavior.

## Documentation Map

- `README.md` is the concise public-facing repository overview.
- `LICENSE` contains the MIT terms for the repository.
- `pyproject.toml` is the source of direct runtime and toolbox/host-test
  dependencies. `tools/lint/pyproject.toml` separately owns the Ruff-only host
  environment.
- `requirements.txt`, `requirements-dev.txt`, and `requirements-lint.txt` are
  generated `pip-compile` locks with hashes. `make runtime-venv` explicitly
  installs the runtime one; the toolbox and ephemeral host-test venv install
  the development one; the host lint venv installs the lint one. Never
  hand-edit a lock or drop its pip-compile header.
- `doc/getting-started.md` covers installation, Codex and Claude Code setup,
  connection modes, options, and troubleshooting.
- `doc/tools.md` defines the MCP tools, command and transfer behavior, and
  public error contract.
- `doc/security.md` defines trust boundaries, SSH isolation, local containment,
  sudo behavior, cleanup, and limitations.
- `doc/architecture.md` explains processes, lifecycle, data paths, components,
  design decisions, repository layout, and future scope.
- `doc/development.md` defines local checks, dependency updates, and the
  automatic and FIDO-assisted live container workflows.
- `doc/contributing.md` is the human contributor guide: environment setup, the
  change loop, review expectations, and pull request requirements. Follow it
  when preparing a change on someone's behalf.
- `doc/examples/` contains complete Codex and Claude Code client examples.
- `Makefile` is the supported development and validation interface.
- `.github/workflows/ci.yml` is the SHA-pinned pull-request and `main` CI,
  including CodeQL, the automatic live Podman job, and the pull-request coverage
  and dependency reports.
- `.github/scripts/pr-comment.sh` publishes one sticky pull-request comment per
  report marker.
- `.github/scripts/annotate-diagnostics.sh` turns gcc-style tool output into
  workflow annotations on the diff and is a pass-through outside CI.
- `.github/dependabot.yml` schedules grouped Python and GitHub Actions updates.
- `.github/CODEOWNERS` assigns `@kogeler` as the default owner of every path.

Keep documentation and examples synchronized with CLI options, MCP schemas,
annotations, and security behavior. Keep detailed material out of the root
README.

## Code Map

- `remote-ssh-mcp` validates the explicitly installed runtime venv and starts
  the Python entry point. It must never create an environment or invoke pip.
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
  live container coverage.
- `tests/live_harness.py` is the thin live CLI for both key modes.
  `tests/live_support/` owns process supervision, keys, Podman topology,
  connection material, orchestration, cleanup, and confinement proofs.
- `tests/live_podman_e2e.py` drives the shared MCP test matrix.
- `make/container.mk` owns container policy: images, confinement flags, and
  the content-addressed image cache.
- `containers/` holds the toolbox/resolver stages, live-target image, and their
  entry points.
- `tools/lint/pyproject.toml` defines the only third-party tool allowed in the
  host lint venv: the self-contained Ruff wheel.

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
make format
make check
make ci
```

Run `make runtime-venv` explicitly before using the MCP server from an agent
configuration. Ordinary development checks do not depend on that environment.

After intentionally changing dependencies, update only direct versions in the
owning `pyproject.toml`, run `make lock`, and review all three regenerated
locks. Use `make refresh-dependencies` to move the whole tree to current
versions instead. Never hand-edit a lock. Keep pytest parallel-safe because the
default suite runs through pytest-xdist workers.

Every pytest run measures branch coverage and fails below
`[tool.coverage.report].fail_under` in `pyproject.toml`, which is the only
place that stores the threshold. Raise it when the suite improves; never lower
it to make a red build green.

When a failure's own output explains nothing - a command produces correct
output yet never finishes, a timeout fires with no diagnostic, a process
disappears without a message - go to a tracer immediately. Do not rebuild the
failing path as a smaller reproduction and do not vary the environment by
trial: a reproduction that omits the broken step proves nothing, and each
variation costs a full run.

`strace -f -e trace=execve,wait4,exit_group,kill` on the real process answers
this class of question in one run. `exit_group(127)` means a program was not
found; `ENOENT` on an `execve` names it. Inside a container a tracer needs
`--cap-add=SYS_PTRACE`, `--security-opt seccomp=unconfined`, and root.

Local tests must not use a real SSH identity. Keep process fakes strict enough
to prove argument vectors, one-authentication behavior, cleanup, and no-fallback
semantics.

Run `make runtime-venv` as a separate explicit prerequisite before either full
live workflow; the live targets do not install it. `make live-test` is
unattended and uses a newly generated ephemeral key.
`make live-fido-test` may invoke a hardware-token PIN dialog and requires the
operator checkpoint. Both create and delete a container, so run them only on a
host with rootless Podman available. Pass FIDO key paths at runtime;
never embed them in source, documentation, fixtures, logs, or Git. See
`doc/development.md` for the complete procedure and cleanup contract.
