# Changelog

All notable Remote SSH MCP changes are recorded here. Changes to the external
SSH transport library have their own changelog.

## Unreleased

### Changed

- Moved all exact direct Python versions to native
  `requirements*.in`/`requirements*.txt` pip-compile pairs so Dependabot can
  update each complete graph instead of treating `pyproject.toml` as a plain
  requirements manifest.
- Aligned both local lock resolvers with Dependabot's pip and pip-tools pair,
  and made the published wheel derive its runtime metadata from
  `requirements.in` without a second version authority.
- Made ordinary CI accept maintenance at an already published version while
  still requiring a new or unpublished version to advance beyond published
  history.
- Made every direct `main` push inspect exact release state, validate an
  existing release against its immutable tagged source, and skip reusable CI
  and publication when that release is already complete.
- Clarified and regression-tested PR-body mirroring of a populated
  `Unreleased` section without requiring a version change.

## 0.2.0 - 2026-08-29

### Changed

- Published the complete documentation through a GitHub Pages workflow whose
  rendering and audit make no network requests after preparing the dedicated
  hash-locked MkDocs environment.
- Added a trusted changelog-to-PR workflow that updates only one managed body
  section while preserving contributor-written text.
- Made the complete five-lock dependency license policy a blocking local gate,
  shared its package-scoped policy with GitHub Dependency Review, kept versions
  solely in generated locks, and prevented exceptions from hiding non-GPL
  package metadata locally.
- Added native amd64 and arm64 standalone Linux executables which CI builds,
  verifies, and smoke-tests before attaching the exact bytes and checksums to
  a release.
- Enforced the documented regular-file boundary before remote range reads.
- Enforced the documented directory boundary before remote directory listings.
- Made exact-length final range reads report `eof=true` without returning an
  extra probe byte.
- Bound the prepared source runtime to the project `.version` as well as its
  dependency lock, removing a release-specific version literal from the
  environment validator.
- Replaced the in-repository SSH wrapper build with the hash-locked
  published `ssh-wrapper==0.1.0` wheel and raised the minimum interpreter to
  CPython 3.13 to match that dependency; CPython 3.13 and 3.14 are the two
  supported interpreter releases.
- Derived the local file boundary from the MCP project containing the launcher
  while preserving relative-path containment and private artifact modes.
- Separated the MCP server from other consumers of the external SSH transport
  library so its packaging, tests, documentation, and release metadata stand
  on their own.
- Restored the repository root as the sole Remote SSH MCP project and activated
  its root-native CI, dependency-submission, and release workflows.
- Bounded every disposable container to a 2,048-ID rootless namespace and
  assigned OpenSSH's privilege-separation account a representable low group so
  the real SSH live matrix remains functional without exhausting host ID maps.

## 0.1.1 - 2026-08-24

### Fixed

- Recovered only the narrow graphical, D-Bus, askpass, and SSH-agent session
  environment needed for hardware-backed OpenSSH from sanitized stdio clients.
- Mapped unavailable interactive prompt routing to a stable path-free error.

## 0.1.0 - 2026-08-19

### Added

- Added the bounded MCP server for commands, structured inspection,
  passwordless sudo, and resumable verified transfers over one explicitly
  authenticated OpenSSH master.
- Added automatic ephemeral-key and operator-controlled hardware-key live
  acceptance for OpenSSH, rsync, sudo, cleanup, and transport loss.

### Security

- Confined local paths to the project containing the launcher, bounded all
  command capture, disabled secondary SSH authentication, and made cleanup
  selective to resources owned by the server process.
