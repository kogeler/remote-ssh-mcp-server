# Release Contract

`.version` is the only human-maintained stable semantic version. The project
version and `remote_ssh_mcp.__version__` are exact mirrors. After a release,
leave it at the published value while ordinary changes accumulate in the
`## Unreleased` section of `CHANGELOG.md`. A pull request does not imply a new
release, and multiple merged pull requests may share that section without
changing package metadata or invoking publication.

CI compares the proposed `.version` with the exact base commit. A changed
version must be strictly greater. An unchanged version is valid when its stable
release is already published; if it is not yet published, it must still be
greater than the latest stable release, which permits an interrupted release
to be retried without weakening progression.

When a maintainer deliberately prepares a release, advance `.version`, move
the accumulated notes into one matching dated section, retain the empty
`## Unreleased` heading, and run `make version-sync`. `make version-check`
rejects drift, duplicate or missing sections, malformed dates, and repository
metadata mismatches. `make release-notes` renders the exact release body with
a tag-stable link to this repository.

When a pull request changes `CHANGELOG.md`, repository automation copies the
newest level-two section containing a bullet entry into one managed block in
the pull-request body. A populated `Unreleased` section is shown even though
`.version` remains unchanged. Contributors may freely edit text outside the
marker block; malformed or duplicate markers fail instead of replacing manual
text.

On every direct push to `main`, the release workflow determines publication
need from exact external state rather than from changed paths. A complete
published match is a no-op even when `main` and `Unreleased` have advanced: its
metadata is checked against the immutable tagged source, while reusable
release CI and publication are skipped. A new release or an exact recoverable
draft reuses the complete CI workflow. Unexpected, incomplete, moved-tag, or
byte-conflicting state fails without deletion or overwrite. Only the final
publication job receives `contents: write`; it creates or validates one
exact-version tag and non-draft, non-prerelease release.

The release contains exactly the `remote-ssh-mcp-linux-amd64` and
`remote-ssh-mcp-linux-arm64` executables produced and smoke-tested natively by
the Ubuntu 26.04 CI jobs, plus `SHA256SUMS.txt`. Publication stages an exact
draft, verifies uploaded sizes and hashes, and only then makes it public. A
rerun may recover only a byte-identical draft; an unexpected asset, tag target,
checksum, or release body fails closed.
