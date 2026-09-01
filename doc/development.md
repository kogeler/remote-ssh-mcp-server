# Development

## Project Boundary

Run commands from this directory. No source or test may import another project
tree or its test infrastructure.

## Local Workflow

```bash
make runtime-venv
make format
make check
```

The runtime environment is separate from development tooling. The launcher
checks its recorded runtime lock and project version but never creates or
modifies the environment.
Unit tests use fake programs and must not access the network or a real SSH
identity.

The production launcher executes the installed module with Python isolated
mode. That module derives the local file boundary from the marked project which
directly owns the active venv. A global interpreter, a detached venv, the
current directory, and the package's `site-packages` location are not accepted
as substitute roots.

`make test` runs the ordinary parallel pytest suite with branch coverage.
`make host-tests` runs the explicitly marked launcher cases. `make check`
combines formatting, lint, strict typing, Bandit, tests, network and container
confinement proofs, lock reproducibility, compatibility, policy, workflow,
version, Python compilation, shell validation, and a network-free wheel/source
archive build. The package gate installs the wheel into an isolated target and
executes its declared `remote-ssh-mcp` entry point outside the source tree,
including runtime-root and configuration validation rather than only `--help`.

`make standalone smoke-standalone` builds the current native Linux executable
with the separate hash-locked PyInstaller environment, validates its ELF
architecture and source/lock provenance, and then runs it from a hostile
directory. The smoke proves version/help/error behavior, clean stdio EOF, and
that private state is created beside the executable rather than in the current
directory or PyInstaller's temporary extraction directory. CI performs this
flow natively on Ubuntu 26.04 runners for both amd64 and arm64.

Render the public documentation site and validate its generated routes with:

```bash
make docs-audit
```

This prepares the separate hash-locked documentation environment, rebuilds
`site/`, and checks local links and anchors, canonical URLs, sitemap forms,
`robots.txt`, `llms.txt`, and deterministic output metadata. Preparing a
missing environment may download its locked wheels; rendering and auditing
make no network requests. Use `make docs-serve` for a local preview; generated
`site/` is never maintained source.

## Published SSH Dependency

`ssh-wrapper==0.1.0` is resolved from PyPI and bound by SHA-256 in every
runtime-derived lock: runtime, development, standalone, and documentation.
Tests and the launcher import only that installed distribution and never add an
alternate source directory to `PYTHONPATH`.

## Tests

Keep process fakes strict enough to prove argv, one authentication, mux-only
reuse, bounds, and cleanup. Tests run concurrently and therefore cannot share
fixed ports or mutable paths. A behavior change must be accompanied by a test
that fails without it and by synchronized documentation.

Live Podman acceptance is intentionally a separate boundary and is not hidden
inside ordinary unit tests. Hardware-key cases remain operator controlled and
must receive key paths only at invocation time.

## Live Acceptance

Prepare the explicit runtime first, then run the unattended matrix:

```bash
make runtime-venv
make live-preflight
make live-test
```

The automatic path generates an ephemeral Ed25519 key, creates one private
Podman network, starts a confined MCP server container and a disposable SSH
target, and proves command, inspection, sudo, transfer, disconnect, master-loss,
and selective-cleanup behavior. The harness removes only resources bearing its
per-run ownership labels.

Hardware-token acceptance keeps the MCP process on the host so OpenSSH can use
the operator's normal authentication UI:

```bash
make live-fido-preflight PUBLIC_KEY=... IDENTITY_FILE=...
make live-fido-test PUBLIC_KEY=... IDENTITY_FILE=...
make live-fido-sanitized-preflight PUBLIC_KEY=... IDENTITY_FILE=...
make live-fido-sanitized-test PUBLIC_KEY=... IDENTITY_FILE=...
```

Every repository-owned file transfer into or out of Podman uses the bounded
deterministic tar protocol in `tools/container_payload.py`. Build and runtime
payloads place exactly this repository beneath the fixed `remote_ssh_mcp/`
archive prefix; bind mounts, named volumes, and `podman cp` are forbidden.

## Debugging Silent Process Failures

When output is correct but a process hangs or disappears without a useful
diagnostic, trace the real path immediately:

```bash
strace -f -e trace=execve,wait4,exit_group,kill -o trace.log <command>
```

An `execve` `ENOENT` identifies a missing program; `exit_group(127)` is the
corresponding shell outcome.
