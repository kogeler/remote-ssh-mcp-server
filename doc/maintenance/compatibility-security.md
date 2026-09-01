# Compatibility and Security Contract

CPython 3.13 and 3.14 are the only supported interpreter releases. CI runs the
ordinary quality, package, and launcher gates with Python 3.14, while `make
compatibility-python` resolves the development environment and executes the
full suite in a disposable Python 3.13 image. Runtime compatibility is
behavioral: required OpenSSH, rsync, Linux process, and filesystem capabilities
are validated rather than approximated by an SSH or distribution version
number.

`make test-network-block` proves the ordinary toolbox cannot reach the
network. `make confinement-test` proves the non-root, read-only, capability,
namespace, environment, and host-socket boundaries. Dependency resolution is
the only toolbox operation permitted to use an online container.

Dependency licenses are deny-by-default outside the project-wide SPDX set in
`.github/dependency-review-config.yml`. GPL-family findings may be acknowledged
only for a reviewed package identity and are never promoted into the global
allow-list. Package versions remain exclusively in the generated locks. Such
an exception cannot hide another unapproved license in the same package's
installed metadata. `make licenses` validates this boundary locally for every
locked environment. GitHub Dependency Review consumes the same configuration,
but its exception is package-wide, so a changed finding for an excepted package
still requires review.

Changes must preserve the public error boundary, one-authentication and
mux-only transport rules, bounded output, passwordless non-caching sudo,
local-path confinement below the selected root, atomic verified transfers, and
selective cleanup. Security checks may be strengthened but never bypassed to
make CI green.
