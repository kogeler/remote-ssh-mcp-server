# Remote SSH MCP Agent Guide

This repository contains only the Remote SSH MCP project. Do not import application code,
tests, documentation, tooling, or generated artifacts from another project
tree. The SSH runtime library is the exact published
[`ssh-wrapper==0.1.0`](https://pypi.org/project/ssh-wrapper/0.1.0/) dependency.

## Project Map

- `remote_ssh_mcp/` contains the Python MCP server.
- `remote-ssh-mcp` validates the explicit runtime environment and executes the
  installed module in isolated mode.
- `remote-ssh-mcp.py` is the explicit-root entry point used by streamed live
  payloads.
- `tests/` contains MCP adapter, protocol, command, sudo, transfer, and
  launcher tests. `tests/live_support/` owns the MCP-only Podman harness.
- `containers/`, `make/`, and `tools/container_payload.py` own the autonomous
  development images, disposable SSH target, live recipes, and pipe transport.
- `doc/` contains all public and maintainer documentation for this project.
- `pyproject.toml` owns project metadata, direct dependencies, typing, security
  scanning, and coverage policy.
- `requirements.txt`, `requirements-dev.txt`, `requirements-lint.txt`,
  `requirements-standalone.txt`, and `requirements-docs.txt` are generated
  hash locks. Never edit them by hand.
- `tools/*standalone*.py` owns native Linux executable build, provenance,
  validation, and smoke behavior. `tools/checksums.py` owns its release
  inventory.
- `.github/scripts/` owns exact dependency and license audits, dependency
  snapshots, changelog-to-PR rendering, and release-version policy. `.github/workflows/`
  contains the active CI, documentation Pages, PR metadata,
  dependency-submission, and release workflows for this repository.
- `doc/maintenance/` records the implemented CI, dependency, development,
  compatibility, security, and release contracts.

## Invariants

- Startup performs no SSH authentication; only `connect` chooses authority.
- One server owns at most one OpenSSH master and never reconnects after loss.
- Every secondary channel is mux-only and cannot authenticate independently.
- `sudo_exec` always uses `sudo -n -k` and never accepts a password or PIN.
- Local paths remain relative to the verified project root for the source
  launcher or the executable directory for a standalone binary and cannot
  escape that boundary.
- Public output and diagnostic tails remain bounded; explicit full-output
  spools stay below the protected local root. Public errors expose no private
  local paths, raw OpenSSH diagnostics, secrets, or internal argv.
- Timeout, cancellation, disconnect, EOF, SIGINT, and SIGTERM clean up only
  owned work.
- Container payloads contain only this project; never add bind mounts, named
  volumes, or `podman cp`.
- Every runtime container uses `--userns=auto:size=2048`; never use unbounded
  `auto`, `keep-id`, `nomap`, or `host` mode.
- Keep all documentation and client examples synchronized with public schemas.
- Runtime and tests install `ssh-wrapper` from PyPI through the canonical hash
  locks; never add an alternate source path or editable dependency install.
- The launcher uses Python isolated mode for its import probe and entry point;
  inherited `PYTHONPATH` or user-site packages cannot select runtime code.
- The prepared runtime is bound to both `requirements.txt` and `.version`.
  Never replace this with a hard-coded Remote SSH MCP version in the launcher
  or Makefile.
- An implicit runtime root is only the marked project directly owning the
  active venv, and its `.version` must match the active package. Never derive
  the local boundary from `site-packages` or cwd.
- Every external workflow action is pinned to one full commit SHA. Default
  workflow permission is read-only; write permission belongs only to the
  smallest trusted job that requires it.
- The privileged PR metadata workflow executes only trusted default-branch
  code. Pull-request `CHANGELOG.md` content is bounded inert input and may
  replace only one marker-delimited body section.
- Documentation is rendered with the dedicated hash lock. After that locked
  environment is prepared, rendering and audit make no network requests;
  Pages deploys only from a trusted `main` push.
- Treat every operator-authored working-tree change as authoritative. Never
  alter, revert, reformat, rename, or otherwise normalize it without the
  operator's explicit agreement in the current conversation. If it conflicts
  with another contract, stop and ask instead of choosing one silently.
- Tests may enforce workflow behavior, permissions, and security boundaries,
  but must not pin human-facing workflow or job `name` text or exact YAML
  serialization.

## Workflow

```bash
make runtime-venv
make format
make check
```

After changing direct dependencies, run `make lock` and review all five
generated locks. Use `make refresh-dependencies` only for an intentional
whole-tree upgrade. Released-product changes require `.version`, its mirrors,
and one matching dated `CHANGELOG.md` section.

Never use a real SSH identity in unit tests. Do not commit or push unless the
operator explicitly requests the corresponding action.

Run `make runtime-venv` before `make live-test` or a FIDO live target. The
automatic live test creates its own key; FIDO key paths are supplied only on
the command line and never stored in source, fixtures, or logs.
