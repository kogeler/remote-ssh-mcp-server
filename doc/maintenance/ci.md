# Continuous Integration Contract

The workflows under `.github/workflows/` are active at the repository root.
They never check out another source repository: `ssh-wrapper==0.1.0` is
resolved only from the hash-locked published dependency graph.

CI grants read-only contents permission by default, pins every external action
to a full commit SHA, and persists no checkout credentials. The quality job
runs `make ci` with Python 3.14; that aggregate also executes the complete
suite in the disposable Python 3.13 compatibility image. Pull requests receive
dependency review. The same reviewed configuration drives that GitHub check
and the blocking local five-environment license gate inside `make ci`; the
local gate resolves every package version only from the generated locks and
rejects an exception whose package is no longer present. CodeQL receives
`security-events: write` only in its own job. The automatic live job runs with
Python 3.14 only after quality succeeds, uses a newly generated identity, and
also exercises the isolated production launcher. Separate Ubuntu 26.04 jobs
build and smoke standalone Linux executables natively on amd64 and arm64.
Hardware-token tests remain operator controlled. A separate read-only version
job compares `.version` with the exact base revision and published release
state. An unchanged version is accepted for ordinary maintenance only when it
is already published; an unpublished version must remain newer than published
history so a failed publication can be retried without allowing a downgrade.

The documentation workflow renders and audits the complete site on relevant
pull requests and `main` pushes. Pull requests receive no Pages credentials or
artifact deployment. Only the direct-`main` deploy job receives `pages: write`
and `id-token: write`, and it publishes the exact site produced by the audited
build job.

Dependency submission and release run only on trusted direct pushes to
`main`. Their write permissions are scoped to the single job that submits the
validated dependency graph or publishes the exact release.
The release caller preserves the exact executables produced by those CI jobs;
the publication job downloads rather than rebuilds them. On every direct
`main` push, the release workflow performs a read-only external-state
inspection. A complete release for the current version is verified against its
immutable tagged source and skips both reusable release CI and publication,
even when `main` and `Unreleased` have advanced. A new or recoverable draft
release alone runs those jobs.

A separate `pull_request_target` metadata workflow runs only when a pull
request changes `CHANGELOG.md`. It checks out trusted `main` code, reads the
exact head changelog through the GitHub API as bounded inert data, and updates
only one marker-delimited section of the pull-request body. Manual text outside
that section is preserved. A populated `Unreleased` section is copied without
requiring a version change. The workflow re-reads the body immediately before
writing and refuses to overwrite a concurrent edit; it never checks out or
executes pull-request code and is the only workflow granted
`pull-requests: write`.
