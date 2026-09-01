# Release Contract

`.version` is the human-maintained stable semantic version. The project
version and `remote_ssh_mcp.__version__` are exact mirrors. `CHANGELOG.md`
contains one dated section for the current version with at least one entry.
Ordinary post-release changes belong in `Unreleased`; they do not change any
version mirror and do not invoke release publication.

For a released-product change, update `.version`, run `make version-sync`, and
add the matching changelog section. `make version-check` rejects drift,
duplicate or missing sections, malformed dates, and repository metadata
mismatches. `make release-notes` renders the exact release body with a
tag-stable link to this repository.

When a pull request changes `CHANGELOG.md`, repository automation copies the
newest level-two section containing a bullet entry into one managed block in
the pull-request body. Contributors may freely edit text outside the marker
block; malformed or duplicate markers fail instead of replacing manual text.

On a direct push to `main` that changes `.version`, the release workflow first
determines whether that commit already has an exact release. A new release
reuses the complete CI workflow. A push that changes only source, tests,
automation, documentation, or `Unreleased` notes cannot run publication. Only
the final publication job receives `contents: write`; it creates or validates
one exact-version tag and non-draft, non-prerelease release.

The release contains exactly the `remote-ssh-mcp-linux-amd64` and
`remote-ssh-mcp-linux-arm64` executables produced and smoke-tested natively by
the Ubuntu 26.04 CI jobs, plus `SHA256SUMS.txt`. Publication stages an exact
draft, verifies uploaded sizes and hashes, and only then makes it public. A
rerun may recover only a byte-identical draft; an unexpected asset, tag target,
checksum, or release body fails closed.
