# Development

## Local Workflow

Use Make as the supported interface. Ruff is installed from a hash-verified
lock into the ignored `venv-lint/` so editors and `make format` can run it on
the host. Checks that need the project environment run in a confined toolbox
container instead:

```bash
make format
make check
```

`make check` runs host Ruff lint and format validation, then runs strict mypy,
Bandit, pytest, Python and Bash syntax, ShellCheck, and exact dependency-freeze
validation in containers. `make ci` adds the online vulnerability scan. The
work tree reaches containers as a tar stream; it is never bind-mounted.

Pytest uses `pytest-xdist` with `-n auto --dist=worksteal` and normally runs
offline in the toolbox.

### Host Exposure

Creating `venv-lint/` still asks the host Python and pip to install third-party
code. The contour is deliberately narrow: the lock contains one self-contained
Ruff wheel, installation requires a recorded hash, and source distributions are
refused. This is a smaller exposure than installing the runtime and test tree,
but it is not zero.

`make runtime-venv` is an explicit host installation, not a launcher side
effect. It creates persistent `venv-runtime/`, installs only `requirements.txt`
as hash-verified wheels, removes the bootstrap pip copy, and records that lock.
The launcher refuses to run if the environment is absent or stale and never
invokes pip itself.

`make host-tests` is the narrower exception needed to drive Podman and real
launcher subprocesses. It installs `requirements-dev.txt` into a temporary venv
below `${TMPDIR:-/tmp}` and removes the entire environment on success, failure,
or interruption. It never modifies `venv-runtime/`.

## Coverage

Every pytest run measures branch coverage of `remote_ssh_mcp` through
`pytest-cov` and fails when the total drops below the gate, so `make check` and
`make ci` already enforce it. `[tool.coverage.report].fail_under` in
`pyproject.toml` is the only place that stores the threshold; the Makefile and
CI read it from there.

The gate is `75`, chosen against a measured total of about `77.1%`. Repeated
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
subprocess and live container tests, which run outside the coverage process.

The test suite includes isolated configuration and path tests, fake OpenSSH and
rsync processes, real MCP STDIO sessions, signal and cleanup behavior, explicit
runtime validation, and dependency refresh.

### The `host` Marker

A few tests need the host itself rather than the code: they drive Podman on the
machine, or they start real launcher subprocesses. Neither is
available or meaningful inside a container, so they carry `@pytest.mark.host`
and `pytest.ini` deselects them by default. The ordinary suite therefore runs
offline in a container with nothing deselected for lack of a tool.

`make host-tests` runs exactly that set, and `make live-fido-test` depends on
it: the operator-run workflow is where host-level behaviour belongs.

A marked test must still leave nothing behind. The host-test venv is ephemeral,
and the launcher uses the separately prepared runtime without modifying it.

## Continuous Integration

`.github/workflows/ci.yml` runs for pull requests targeting `main` and pushes to
`main`. Its workflow has four jobs; the live job starts only after quality
passes:

- `make ci` runs Ruff, strict mypy, Bandit, parallel pytest, syntax and shell
  checks, freeze validation, and `pip-audit` against the installed pinned tree;
- Dependency Review rejects pull requests that introduce dependencies with a
  known vulnerability of moderate or higher severity;
- CodeQL analyzes Python with the `security-extended` query suite and uploads
  results to GitHub code scanning;
- the live job verifies that the runner has rootless Podman and executes the
  automatic ephemeral-key `make live-test` after the quality job succeeds.

The quality job restores or saves toolbox and live-target OCI archives through
`actions/cache`. The live job currently runs on a separate runner, does not
restore that archive, and builds or reuses its target image locally.

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

Bandit, pip-audit, ShellCheck, the freeze comparison, and the live run stay
log-only. They fail rarely, and their output is not addressed to a single line.

Both comment paths need `pull-requests: write`, which the two jobs request
individually so the workflow default stays `contents: read`. Dependabot-created
pull requests are covered by that grant, but a pull request from a fork always
receives a read-only token regardless of the requested scope. Neither path may
turn that into a failed check: the coverage step is `continue-on-error`, and
Dependency Review already downgrades an unwritable pull request to a warning. A
missing comment therefore never changes a check result, and the identical
report always remains in the job summary.

The live job needs no host firewall changes. Rootless Podman routes container
traffic through its user-space network stack, so the workflow needs neither a
privileged bridge nor forwarding-policy changes.

Dependency Review additionally requires the repository dependency graph. While
it is disabled the job fails with `Dependency review is not supported on this
repository`, independently of the pull request contents.

All third-party `uses:` references are pinned to full commit hashes and retain
the release version in a comment, in every workflow. Dependabot checks both
Python dependencies and GitHub Actions weekly; see the dependency section below
for why its Python updates are restricted. Dependency Review and CodeQL result
upload require a public repository or GitHub Advanced Security when the
repository is private. All jobs select Ubuntu 26.04 explicitly.
`.github/actionlint.yaml` registers that runner label for actionlint `v1.7.12`
without suppressing any other workflow checks.

The local equivalent, excluding GitHub-only Dependency Review and CodeQL result
upload, is:

```bash
make ci
make runtime-venv
make live-test
```

## Dependencies

Two manifests are human-maintained sources for direct dependencies:

- `[project].dependencies` contains packages imported by runtime code;
- `[project.optional-dependencies].dev` contains direct test, type-check, and
  security-audit tools used by the toolbox and host tests;
- `tools/lint/pyproject.toml` contains Ruff alone, without the runtime tree;
- exact versions make dependency review explicit and easy to update.

Three lock files are generated by `pip-compile`, and none is ever edited by
hand:

- `requirements.txt` resolves `[project].dependencies` only. The explicit
  `make runtime-venv` target installs this one, so an installed server carries
  no linter, type checker, or test runner. The launcher only validates and uses
  that prepared environment.
- `requirements-dev.txt` resolves the same tree plus the `dev` extra. The
  toolbox installs it; `make host-tests` uses it in one ephemeral host venv.
- `requirements-lint.txt` resolves `tools/lint/pyproject.toml` and contains only
  Ruff. `venv-lint/` installs it as wheels with mandatory hashes.

All locks pin every transitive package and carry `--hash` lines for each one.
Hashes put pip into hash-checking mode, so an artifact that does not match the
recorded digest is refused rather than installed. The locks exclude the project
itself, so none contains a machine-specific local path.

After changing a version in either manifest, recompile:

```bash
make lock
make check
```

`make lock` respects the pins that already satisfy `pyproject.toml`, so it
changes only what your edit forced. To move the whole tree to current versions
instead, request an upgrade resolution:

```bash
make refresh-dependencies
make check
```

That target uses the isolated resolver image to recompile all three locks with
`--upgrade`, deletes the persistent runtime and Ruff environments, and
recreates only the Ruff venv. Reinstall the runtime explicitly with
`make runtime-venv` before using the server again; host-test environments are
always ephemeral.

`make freeze-check` recompiles all three locks into temporary copies and compares
them, ignoring comments. Because it does not pass `--upgrade`, a newly released
version elsewhere on PyPI cannot make it fail; only a lock that no longer
matches its manifest can. It needs network access.

Pip-tools itself lives in a minimal resolver stage rather than the main
toolbox. That keeps the check image smaller and allows `make lock` to repair a
development lock that can no longer build the toolbox image. Because a resolver
cannot bootstrap from the lock it generates, its complete small dependency tree
is pinned inline in the Containerfile and restricted to wheels. Full hash
refreshes get a separate bounded 4 GiB temporary/memory allowance; ordinary
checks retain the tighter toolbox limits.

### Automated Updates

Dependabot recognises `pip-compile` output by the header the tool writes, and
recompiles the whole tree rather than editing one pinned line. That distinction
matters: a single edited line in a resolved tree is usually unsatisfiable.
`pydantic` requires an exact `pydantic-core`, and `pydantic-core` publishes
releases ahead of the stable `pydantic` that consumes them, so a lone bump of
that transitive package fails in pip before any check can run.

Never remove the header, and never add `--no-header` to the compile flags. It is
the only thing that tells Dependabot how the file was produced.

The runtime and development locks keep the complete transitive tree in files
GitHub parses into the dependency graph. The graph is what gives Dependabot
alerts and Dependency Review visibility into transitive packages, so shrinking
these files to direct dependencies would silence the security signal along with
the noise.

## Documentation And Schemas

When changing a CLI option, tool, annotation, or security invariant, update the
matching files under `doc/`, the examples under `doc/examples/`, and their
schema/configuration tests. Keep `README.md` concise; details belong here.

## Live Container Tests

Both live workflows build or reuse the content-addressed target image, run the
server and matrix driver on the host, and connect them to one disposable target
container through a random port bound only to `127.0.0.1`. They remove the
container and every temporary file on success, failure, or interruption. Images
are retained as a build cache and can be removed with `make clean-containers`.
The workflows need rootless Podman and no host privilege: no daemon, bridge, or
`sudo`.

The automatic workflow creates an unencrypted Ed25519 key in a mode-`0700`
temporary directory, installs only its public half in the target, and deletes
both key files at exit. It needs no SSH prompt or operator action and is
suitable for a CI runner:

```bash
make runtime-venv
make live-preflight
make live-test
```

The installation target is a separate prerequisite. Neither `live-test` nor
`host-tests` invokes it implicitly.

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

The image is defined by `containers/live-target/` and pinned to a base image
digest, so a run is reproducible and Dependabot can propose the base update.

### Why The Target Is Confined The Way It Is

The target runs as root inside rootless Podman's user namespace because it has
to start sshd, manage the test account, and honour sudo. That root has no host
root authority, no host path is mounted, and all capabilities remain namespaced.

Three hardening options are deliberately absent, and none should be added:

- `--userns=auto` is used by the toolbox but cannot be used for the live target:
  sshd privilege separation calls `setgroups()` with a gid outside the narrow
  auto mapping and fails before authentication;
- `--security-opt=no-new-privileges` would neutralise the setuid `sudo` binary,
  and passwordless sudo is part of the contract under test;
- `--read-only` would stop sshd writing to `/run` and stop the matrix
  installing a sudo policy per case.

Everything else is dropped or bounded. The container starts with
`--cap-drop=ALL` and regains only what the matrix provably needs: `SETUID`,
`SETGID`, and `SYS_CHROOT` for sshd and sudo, `CHOWN`, `DAC_OVERRIDE`, and
`FOWNER` to prepare fixtures, `KILL` for cleanup, `AUDIT_WRITE` for login
records, `NET_BIND_SERVICE` to bind port 22, and `NET_ADMIN` for the `tc`
rate limit that makes the cancel-and-resume transfer test deterministic.

Podman rewrites `--cap-drop=ALL` into a delta against its own default set, so
the declared configuration proves nothing. The harness reads `CapEff` from
`/proc` inside the running container and fails if a forbidden capability is
present or a required one is missing.

The SSH port is published on `127.0.0.1` with a random port. Any local user can
reach that port while either test runs; authentication is public-key only, with
a newly generated ephemeral key in the automatic mode.

The matrix covers disconnected startup, strict schemas, commands, timeouts,
bounded and binary output, inspection, unusual filenames, NOPASSWD sudo,
password/cache refusal, policy denial, large upload/download, overwrite,
cancellation, resume, hashes, concurrency, one authentication, one transport,
master loss, and explicit disconnect. Cleanup removes the container and every
temporary authentication, host-key, runtime, and transfer artifact on success
or failure.
