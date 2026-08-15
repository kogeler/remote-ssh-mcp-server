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

## Coverage

Every pytest run measures branch coverage of `remote_ssh_mcp` through
`pytest-cov` and fails when the total drops below the gate, so `make check` and
`make ci` already enforce it. `[tool.coverage.report].fail_under` in
`pyproject.toml` is the only place that stores the threshold; the Makefile and
CI read it from there.

The gate is `75`, chosen against a measured total of about `76.9%`. Repeated
runs are usually identical, but timing-sensitive branches in the transfer and
master code can move the total by a few hundredths, so the gate keeps a margin
for that and for refactors while still failing on a real regression. Raise it
deliberately when the suite improves; never lower it to make a red build green.

`make coverage-report` renders the last run as Markdown, which is what CI writes
to the job summary and to the pull-request comment:

```bash
make test
make coverage-report
```

The lowest-covered modules are `server.py` and `cli.py`, because the MCP STDIO
main loop and argument-parser entry point are exercised mostly through the
subprocess and live LXC tests, which run outside the coverage process.

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

Two of those jobs also report into the pull request itself:

- the quality job writes the Markdown coverage report to the job summary and,
  on pull requests, publishes it through `.github/scripts/pr-comment.sh` as one
  sticky comment that later pushes rewrite in place;
- Dependency Review posts its own summary of added, updated, and removed
  dependencies with their licenses and known vulnerabilities.

CodeQL needs no comment step: its findings appear as inline annotations on the
pull-request diff and in the repository code-scanning view. Ruff and mypy use
the same channel instead of a comment, because a diagnostic is only useful next
to the line that caused it. CI sets `RUFF_OUTPUT_FORMAT=github` so Ruff renders
workflow annotations directly, and `make typecheck` pipes mypy through
`.github/scripts/annotate-diagnostics.sh`, which converts gcc-style diagnostics
into annotations and stays a transparent pass-through outside GitHub Actions.
The pipeline keeps `pipefail`, so annotating never hides a failing tool.

Bandit, pip-audit, ShellCheck, the freeze comparison, and the live LXC run stay
log-only. They fail rarely, and their output is not addressed to a single line.

Both comment paths need `pull-requests: write`, which the two jobs request
individually so the workflow default stays `contents: read`. Dependabot-created
pull requests are covered by that grant, but a pull request from a fork always
receives a read-only token regardless of the requested scope. Neither path may
turn that into a failed check: the coverage step is `continue-on-error`, and
Dependency Review already downgrades an unwritable pull request to a warning. A
missing comment therefore never changes a check result, and the identical
report always remains in the job summary.

The live job opens the host firewall for the LXD bridge before it runs. The
hosted runner image ships a running Docker daemon, which sets the `FORWARD`
policy to `DROP`. Bridged instance traffic is forwarded traffic, so without that
step the test instance resolves names through `lxdbr0` and then reaches no
archive at all, and provisioning fails inside apt rather than in anything this
project owns.

Dependency Review additionally requires the repository dependency graph. While
it is disabled the job fails with `Dependency review is not supported on this
repository`, independently of the pull request contents.

All third-party `uses:` references are pinned to full commit hashes and retain
the release version in a comment, in every workflow. Dependabot checks both
Python dependencies and GitHub Actions weekly; see the dependency section below
for why its Python updates are restricted. Dependency Review and CodeQL result
upload require a public repository or GitHub Advanced Security when the
repository is private.
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

### Automated Updates

Dependabot edits one pinned line at a time, which is exactly the hand-edit the
rule above forbids. In a full freeze every transitive package looks like a
direct requirement, and raising one on its own can be unsatisfiable: `pydantic`
requires an exact `pydantic-core`, and `pydantic-core` publishes releases ahead
of the stable `pydantic` that consumes them. A freeze bumped that way fails in
pip before any check runs.

Two mechanisms keep that from happening, without hiding anything from GitHub:

- `.github/dependabot.yml` allows only the names pinned by hand in
  `pyproject.toml`, so a transitive package is never proposed on its own.
  `tests/test_repository_layout.py` fails if that list and the pins drift apart,
  so a new direct dependency cannot be forgotten.
- `.github/workflows/dependabot-freeze.yml` rebuilds the freeze with
  `make refresh-dependencies` on Dependabot pull requests and pushes the
  coherent tree onto the same branch. Re-running it on an already coherent
  branch pushes nothing.

`requirements.txt` deliberately keeps its name and its complete transitive
content. That file is what GitHub parses into the dependency graph, and the
graph is what gives Dependabot alerts and Dependency Review visibility into
transitive packages. Renaming it would silence the noise and the security
signal together.

A commit pushed with the default `GITHUB_TOKEN` does not start a new workflow
run, so the regenerated head keeps the check runs of the previous commit.
Re-run the checks manually, or add a Dependabot secret named
`DEPENDABOT_PUSH_TOKEN` holding a token with `contents: write` for this
repository, which the workflow prefers when present.

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
