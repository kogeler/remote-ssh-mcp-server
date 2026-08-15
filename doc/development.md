# Development

## Local Workflow

All Python dependencies live in the repository-local `venv/` and are ignored
by Git. Use Make as the supported interface:

```bash
make venv
make format
make check
```

`make check` runs Ruff lint and format validation, strict mypy analysis, Bandit
security analysis, pytest across automatically selected parallel worker
processes, Python compilation, Bash syntax, ShellCheck when installed, launcher
tests, and an exact dependency-freeze comparison. `make ci` adds the online
known-vulnerability scan performed by `pip-audit`.

Pytest uses `pytest-xdist` with `-n auto --dist=worksteal`. Override the worker
count for constrained environments by passing it explicitly, for example:

```bash
venv/bin/python -m pytest -n 4
```

The test suite includes isolated configuration and path tests, fake OpenSSH and
rsync processes, real MCP STDIO sessions, signal and cleanup behavior, launcher
bootstrap, dependency refresh, and retry after failed installation.

## Continuous Integration

`.github/workflows/ci.yml` runs for pull requests targeting `main` and pushes to
`main`. Its jobs provide four independent gates:

- `make ci` runs Ruff, strict mypy, Bandit, parallel pytest, syntax and shell
  checks, freeze validation, and `pip-audit` against the installed pinned tree;
- Dependency Review rejects pull requests that introduce dependencies with a
  known vulnerability of moderate or higher severity;
- CodeQL analyzes Python with the `security-extended` query suite and uploads
  results to GitHub code scanning;
- the live job installs the current stable LXD snap on a clean Ubuntu 26.04
  runner and executes the automatic ephemeral-key `make live-test` after the
  quality job succeeds.

All third-party `uses:` references are pinned to full commit hashes and retain
the release version in a comment. Dependabot checks both Python dependencies
and GitHub Actions weekly. Dependency Review and CodeQL result upload require a
public repository or GitHub Advanced Security when the repository is private.
Ubuntu 26.04 is selected explicitly and might remain a GitHub public-preview
runner until GitHub promotes the image to general availability. The
`.github/actionlint.yaml` compatibility entry teaches actionlint `v1.7.12`
about this newer official label without suppressing any other workflow checks.

The local equivalent, excluding GitHub-only Dependency Review and CodeQL result
upload, is:

```bash
make ci
make live-test
```

## Dependencies

`pyproject.toml` is the human-maintained source for direct dependencies:

- `[project].dependencies` contains packages imported by runtime code;
- `[project.optional-dependencies].dev` contains direct test, lint, type-check,
  and security-audit tools;
- exact versions make dependency review explicit and easy to update.

`requirements.txt` is generated output. It contains the complete resolved tree,
including transitives, and is the reproducible installer input for the launcher
and CI. It deliberately excludes the project itself so it never contains a
machine-specific local path.

After updating direct versions in `pyproject.toml`, rebuild everything from an
empty environment:

```bash
make refresh-dependencies
make check
```

The refresh target deletes `venv/`, creates it again, upgrades pip, installs the
project with its `dev` extra to resolve the complete tree, removes the project
wheel itself, writes `pip freeze --exclude remote-ssh-mcp` to
`requirements.txt`, updates the launcher's successful-install marker, and
removes temporary build metadata. The resulting environment therefore matches
the frozen installer input exactly and can be audited without a local-project
exception.

Use `make freeze` only to capture the already-installed environment. Never
hand-edit `requirements.txt`; update direct dependencies in `pyproject.toml`
and regenerate it.

## Documentation And Schemas

When changing a CLI option, tool, annotation, or security invariant, update the
matching files under `doc/`, the examples under `doc/examples/`, and their
schema/configuration tests. Keep `README.md` concise; details belong here.

## Live LXC Tests

Both live workflows create a uniquely named Debian LXC instance, run the same
complete MCP black-box matrix, and delete the instance and temporary files on
success, failure, or interruption. They change the local LXC daemon and must be
run only on an authorized, disposable test host.

The automatic workflow creates an unencrypted Ed25519 key in a mode-`0700`
temporary directory, installs only its public half in the test instance, and
deletes both key files at exit. It needs no SSH prompt or operator action and is
suitable for an isolated CI runner with access to LXC:

```bash
make live-preflight
make live-test
```

The FIDO workflow uses an existing hardware-backed OpenSSH key. Key paths are
runtime parameters and must not be embedded in source, documentation, fixtures,
logs, or Git. Its full run pauses immediately before authentication so the
operator can confirm the system PIN dialog and touch the key once:

```bash
make live-fido-preflight \
  PUBLIC_KEY=/path/to/public-key \
  IDENTITY_FILE=/path/to/identity
make live-fido-test \
  PUBLIC_KEY=/path/to/public-key \
  IDENTITY_FILE=/path/to/identity
```

Use `LXC_IMAGE=...` with any of these targets to override the default
`images:debian/13` image.

The shared harness initializes the container with:

```text
-c security.nesting=true
-c security.syscalls.intercept.mknod=true
-c security.syscalls.intercept.setxattr=true
-c security.idmap.size=1000000
-c security.devlxd=false
-c security.idmap.isolated=true
-c linux.kernel_modules=br_netfilter
```

Before starting it, the harness applies this NIC override:

```text
lxc config device override <instance> eth0 security.mac_filtering=true \
  security.ipv4_filtering=true security.ipv6_filtering=true
```

`linux.kernel_modules=br_netfilter` asks the LXC daemon to load the host module
required by bridge IPv4/IPv6 filtering before the instance starts, without a
separate sudo command. The harness verifies every instance and NIC setting
after startup. It installs only the selected public key for the test account,
constructs a protected temporary SSH configuration for the matching identity,
and drives the MCP protocol itself.

The matrix covers disconnected startup, strict schemas, commands, timeouts,
bounded and binary output, inspection, unusual filenames, NOPASSWD sudo,
password/cache refusal, policy denial, large upload/download, overwrite,
cancellation, resume, hashes, concurrency, one authentication, one transport,
master loss, and explicit disconnect. Cleanup removes the container and every
temporary authentication, host-key, runtime, and transfer artifact on success
or failure.
