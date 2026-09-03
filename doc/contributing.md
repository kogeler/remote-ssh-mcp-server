# Contributing

Read [Security](security.md), [Architecture](architecture.md), and `AGENTS.md`
before changing behavior. Open an issue first for a new MCP tool, a public
schema or error change, a relaxed limit, or a lifecycle change.

These are contracts rather than style preferences: startup authenticates
nothing; every operational channel is mux-only; transport loss never silently
reconnects; sudo remains passwordless and cache-independent; public errors do
not leak private paths, subprocess argv, or secrets.

## Environment

Use Linux with CPython 3.13 or 3.14, Bash, rootless Podman, Git, OpenSSH, rsync,
GNU coreutils, and GNU Make. Run commands from this project root. Make is the
supported interface; `make help` lists its main targets.

`make runtime-venv` is the explicit installation step for running the server.
It contains runtime packages only. Development and lint environments are
separate; do not install project requirements into a system or user Python.

## Change Loop

```bash
make format
make check
```

Use strict typing, keep tests parallel-safe, and do not weaken lint, security,
or coverage policy to make a failure green. Unit tests must use fake SSH,
rsync, and sudo programs rather than real identities or hosts.

`make check` includes lint and format validation, strict mypy, Bandit, branch
coverage, syntax and shell checks, network and container confinement,
reproducible locks, minimum-Python compatibility, workflow validation, policy,
and version checks. `make ci` additionally runs the reviewed online dependency
audit.

When changing startup, packaging, or dependencies, also run
`make standalone smoke-standalone` on the current native architecture. CI runs
the same contract independently on amd64 and arm64.

## Review Expectations

- Fix types rather than adding unchecked `Any`, casts, or ignores.
- Do not add lint or security suppressions without a precise local reason.
- Match the surrounding naming, error handling, and comment density.
- Explain why a constraint exists rather than narrating the next line.
- Keep detailed material under `doc/` and the root README concise.

## Tests

Every behavior change needs a test that fails without it. Fakes should assert
exact argv, one authentication, mux-only reuse, output bounds, and cleanup.
The default suite runs through pytest-xdist, so tests cannot depend on order,
fixed ports, or shared mutable paths. Host-dependent launcher and Podman tests
carry the `host` marker and are excluded from ordinary pytest.

Every pytest run enforces the branch-coverage threshold stored only in
`pyproject.toml`. Raise it deliberately when coverage improves; never lower it
to hide a regression. A test must leave no generated state in the checkout.

## Debugging Silent Failures

When correct output is followed by a hang, unexplained timeout, or silent
process exit, trace the real failing path instead of varying it by trial:

```bash
strace -f -e trace=execve,wait4,exit_group,kill -o trace.log <command>
```

`exit_group(127)` indicates a program was not found; `ENOENT` on `execve`
identifies it. A disposable tracing container needs `SYS_PTRACE`, an
unconfined seccomp profile, and root; never weaken the normal test container.

## Live Acceptance

The unattended matrix requires the explicit runtime and creates its own
ephemeral Ed25519 identity:

```bash
make runtime-venv
make live-preflight
make live-test
```

Hardware-token targets remain operator controlled. Supply key paths only as
runtime arguments, never in source, documentation, fixtures, or logs. The
sanitized variant removes the recoverable session allowlist from the MCP child
and proves one authentication through the user-systemd askpass route. It is not
part of CI or another aggregate target.

## Dependencies And Documentation

Direct pins belong in the matching `requirements*.in` file. The development,
standalone, and documentation inputs extend `requirements.in`; the lint input
is independent. Run `make lock` and review all five generated `.txt` locks
after a direct dependency change. Do not hand-edit a lock or add a dependency
when a small standard-library implementation suffices.

Documentation is part of the same atomic change. Synchronize CLI options,
tools, schemas, limits, annotations, errors, security behavior, examples, and
the applicable maintainer contract. Policy tests require local documentation
links to remain relative and resolvable. Run `make docs-audit` after changing
public or maintainer documentation.

Keep a pull request focused on one behavior. Explain the security and cleanup
effects and list the exact validations performed. Documentation and client
examples are part of the same change, not follow-up work.

The SSH library is a published hash-locked dependency, not vendored code or an
alternate import path. Changes to its public API must be validated
independently and against this consumer.

## Pull Requests And Releases

Use an imperative subject and explain the need, security and cleanup effects,
and exact validation. Third-party workflow actions remain pinned to full
commit SHAs. The pull-request workflow has read-only contents permission.
Dependency submission and release run only on trusted direct pushes to `main`;
write access stays confined to the individual job that needs it.

Add release-worthy changes to the existing `## Unreleased` section in
[`CHANGELOG.md`](../CHANGELOG.md). Keep `.version` unchanged in an ordinary
pull request: multiple changes and multiple merged pull requests may
accumulate before a maintainer deliberately prepares a release.

Changing `CHANGELOG.md` triggers the dedicated PR metadata workflow. It copies
the newest populated level-two section—normally `## Unreleased`, even when
`.version` did not change—into a marker-delimited block in the pull-request
body while preserving all manual text. Do not edit or duplicate the marker
lines; edit the changelog source or write outside the managed block.

Only deliberate release preparation advances `.version`: move the accumulated
notes into one matching dated `CHANGELOG.md` section, retain the empty
`## Unreleased` heading, and run `make version-sync`. Validate with
`make version-check` and render the exact release body with `make release-notes`.

Do not report a vulnerability in a public issue. Use private vulnerability
reporting or contact the code owner. Include the affected version, crossed
trust boundary, and a minimal reproduction without credentials, private keys,
or real hostnames.
