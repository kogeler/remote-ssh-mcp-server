# Contributing

Thank you for considering a contribution. This page is the practical guide for
changing this repository: how to set up, what the checks enforce, and what a
reviewable pull request looks like. `doc/development.md` covers the same tools
in more depth; read this first.

## Before You Start

The security invariants are the contract of this project, not a style
preference. Read [Security](security.md) and the required invariants in
`AGENTS.md` before changing runtime behavior. A change that weakens one of them
is rejected regardless of how convenient it is:

- starting the server authenticates nothing, and only `connect` selects
  authority;
- one process owns at most one SSH master, and a lost master never
  reconnects silently;
- every command and transfer reuses the existing mux socket and cannot fall
  back to a new SSH login;
- `sudo_exec` stays passwordless and cache-independent;
- public MCP errors never leak local paths, subprocess arguments, or secrets.

Open an issue before writing code when a change adds or alters an MCP tool,
changes a public schema or error string, relaxes a limit, or touches the
process and cleanup model. Bug fixes, tests, and documentation need no prior
discussion.

## Environment

You need Python 3.11 or newer, OpenSSH, and rsync. ShellCheck is optional
locally and always runs in CI. Everything else is installed into a
repository-local `venv/` that Git ignores:

```bash
git clone git@github.com:kogeler/remote-ssh-mcp-server.git
cd remote-ssh-mcp-server
make venv
```

`make venv` runs the launcher, which creates the virtual environment and
installs the hashed runtime lock, then adds the development lock on top. Never
install project dependencies into a system or user environment, and never
activate an unrelated virtual environment for this repository.

`make help` lists every supported target. Make is the supported interface;
prefer it over calling the tools directly so local runs match CI.

## The Change Loop

```bash
make format
make check
```

`make format` applies Ruff fixes and formatting. `make check` runs Ruff lint and
format validation, strict mypy, Bandit, the pytest suite with its coverage gate,
Python compilation, Bash syntax, ShellCheck when installed, and an exact
dependency-freeze comparison. `make ci` adds the online `pip-audit` scan and is
what the CI quality job executes.

Run `make ci` before opening a pull request. It is the same command CI runs, so
a green local run and a red CI run should differ only in the GitHub-only checks.

## What Reviewers Expect

- **Strict typing.** mypy runs in `strict` mode over `remote_ssh_mcp` and the
  entry point. Do not add `Any`, `type: ignore`, or unchecked casts to silence
  it; fix the type instead.
- **No new lint or security suppressions.** If a Bandit or Ruff rule must be
  skipped, explain why in a comment next to the suppression.
- **Match the surrounding code.** Follow existing naming, comment density, and
  error-handling shape rather than introducing a second style.
- **Comments explain intent.** Describe why a constraint exists, not what the
  next line does.
- **Keep the root clean.** Detailed material belongs under `doc/`; `README.md`
  stays short.

## Tests

Every behavior change needs a test that would fail without it.

- Local tests must never use a real SSH identity, a real host, or the network.
  Drive the fake OpenSSH and rsync processes in `tests/` instead.
- Keep process fakes strict. A useful fake asserts the exact argument vector,
  proves that authentication happens once, and proves that no fallback
  connection is possible.
- Keep tests parallel-safe. The suite runs under `pytest-xdist` with
  `-n auto --dist=worksteal`, so tests must not depend on order, shared
  temporary paths, or a fixed port.
- Coverage is measured on every run and the build fails below the threshold in
  `[tool.coverage.report].fail_under`. That value lives in `pyproject.toml`
  only. Raise it when the suite genuinely improves; never lower it to make a
  red build green.

`make coverage-report` prints the Markdown report for the last run, which is
the same report CI publishes on the pull request.

## Live LXC Tests

The live workflows create and delete a real Debian LXC instance and change the
local LXC daemon. Run them only on a host you are authorized to use for
disposable containers:

```bash
make live-preflight
make live-test
```

`make live-test` is unattended and generates its own ephemeral Ed25519 key.
`make live-fido-test` uses an existing hardware-backed key and pauses for a PIN
and touch. Pass hardware-key paths as runtime parameters only. Never commit a
key path, key material, or a real hostname to source, documentation, fixtures,
or logs. See [Development](development.md) for the full procedure and cleanup
contract.

## Dependencies

`pyproject.toml` is the only file you edit by hand, and only its direct pinned
versions. `requirements.txt` and `requirements-dev.txt` are `pip-compile`
output:

```bash
make lock
make check
```

Never hand-edit a lock; `make check` recompiles both and fails on any drift.
Runtime dependencies belong in `[project].dependencies` and reach every
installed server, so weigh them accordingly; tooling belongs in the `dev` extra
and never leaves a development environment. Justify every new direct
dependency in the pull request. A dependency that a few lines of standard
library code replace will be questioned.

## Documentation

Documentation is part of the change, not a follow-up. When you change a CLI
option, tool, annotation, limit, or security behavior, update the matching
files under `doc/`, the client examples under `doc/examples/`, and their tests
in the same pull request. A test verifies that every relative link in
`README.md` and `doc/` resolves.

## Pull Requests

Keep one pull request to one topic. Write a subject line in the imperative mood
and use the body to explain why the change is needed and what you verified.

CI runs four jobs on every pull request against `main`:

| Check | What it does |
| --- | --- |
| `Lint, test, and audit` | `make ci`, plus the coverage report |
| `Dependency review` | blocks new dependencies with known vulnerabilities |
| `CodeQL` | `security-extended` analysis, annotated on the diff |
| `Live LXC` | the automatic ephemeral-key live run |

Two of them report into the pull request itself: the quality job posts a
coverage comment, and Dependency Review posts a summary of dependency changes.
Both are also written to the job summary, so a missing comment on a fork pull
request is expected and never means a failed check.

Ruff, mypy, and CodeQL findings appear as annotations on the changed lines, so
a failing lint or type check points at the exact line without opening the log.
Everything else, including Bandit, pip-audit, ShellCheck, and the live run,
reports in the job log only.

Every workflow change must keep third-party `uses:` references pinned to a full
commit SHA with the release tag in a trailing comment. A test enforces this.

`@kogeler` owns every path through `.github/CODEOWNERS` and reviews all pull
requests.

## Reporting A Vulnerability

Do not open a public issue for a security problem in this project. Use GitHub's
private vulnerability reporting on the repository, or contact the code owner
directly if that is unavailable. Include the version or commit, the trust
boundary you believe is crossed, and a minimal reproduction. Do not include
real credentials, key material, or hostnames.
