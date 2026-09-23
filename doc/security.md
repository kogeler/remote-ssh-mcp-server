# Security Model

Remote SSH MCP gives an MCP client the authority of one local user and of the
remote SSH accounts it deliberately connects to. It narrows transport, capture, filesystem, and cleanup
behavior; it is not a sandbox for arbitrary remote commands.

## Trust Boundaries

Trusted inputs are the local user and files writable by that user, the chosen
remote SSH account and its programs, system and user OpenSSH configuration,
the MCP client's approval policy, and on Linux the current UID's logind record
and active user systemd environment.

Agent-controlled inputs include commands, remote paths, local-root-relative
paths, overwrite decisions, aliases, and direct host/user/port values.
They are validated, bounded, or quoted at the local process boundary. Remote
filenames and contents remain untrusted data.

OpenSSH configuration is executable configuration: `ProxyCommand`,
`ProxyJump`, `Match exec`, token providers, and similar directives may launch
local programs. Review it before exposing an alias or host to an agent.

## SSH Boundary

- Server startup does not authenticate.
- Each `connect` creates one session owning one foreground OpenSSH
  ControlMaster and a private socket. Sessions authenticate one at a time.
- Before authenticating, `connect` runs `ssh -G` for the same target to learn
  its effective `HostName` and `Port`; like any OpenSSH invocation, this
  evaluates the user's configuration, including `Match exec`. A server that
  another session owns is refused without authenticating.
- Mux clients disable passwords, public-key authentication, host-based
  authentication, GSSAPI, and fallback proxying.
- Forwarding, agent sharing, X11 forwarding, and SSH-configured local and
  remote commands are disabled.
- Master loss is reported and never starts another authentication; only an
  explicit disconnect followed by a new `connect` does.
- Linux session recovery may import only the documented eight routing values;
  non-empty inherited values win. It never imports `PATH`, `HOME`, loader or
  Python variables, credentials, or an arbitrary login environment.
- Recovery is private to the first master subprocess and never changes
  `os.environ` or reaches commands and transfers.

Native OpenSSH remains responsible for host keys, proxies, identities, and
hardware-token prompts. The published `ssh-wrapper` library owns that
transport implementation; this project owns its MCP exposure.

## Session Keys

A session key is a 256-bit random capability returned only in the `connect`
result. The server stores its SHA-256 digest, compares presented keys in
constant time, and never returns, logs, or lists the key. Every operation other
than `connect`, `disconnect`, and `session_list` must present the matching
session ID and key, which prevents another agent sharing the server process,
or the same agent, from silently using the wrong session or server.

The key separates callers of one server process; it is not a secret from the
local user or the MCP client. It travels in tool-call arguments and client
transcripts. `session_list` shows every session's target and state to every
caller, and `disconnect` closes any session by ID without a key, so any caller
can end another caller's session and its active work. Sessions carry no
free-form caller text that other agents would read.

## Local Files

Every model-selected local path is relative to one explicit local root shared
by all sessions: the source project which owns the prepared venv, or the
directory containing a standalone executable. Traversal, NULs, protected
internal paths, and symlink escapes are rejected. Spools and transfer partials use private directories and restrictive
creation modes. Normal access controls of the current local user remain the
outer trust boundary.

The launcher validates and starts the prepared runtime with Python isolated
mode, so inherited `PYTHONPATH` and user-site packages cannot replace locked
dependencies. The installed module verifies that its active virtual
environment lives in the marked MCP project's `.venvs/<machine-user-key>/`
directory for this machine and user, then uses that project as the local
filesystem boundary. The project's `.version` must match the
active package. It never treats `site-packages`, the caller's working
directory, or an ambient import path as that boundary.

Source-checkout environments are scoped to the local machine and UID. The
namespace uses HMAC-SHA-256 with a fixed application-specific key over
`/etc/machine-id` and the UID, following the
[systemd machine-ID guidance](https://www.freedesktop.org/software/systemd/man/latest/machine-id.html).
Neither the raw machine ID nor a substring is placed in directory names or
diagnostics. Missing, invalid, or uninitialized IDs fail closed without falling
back to another host's venv. This is environment isolation for checkouts shared
across machines, not protection against another trusted user with write access
to the checkout or against systems cloned with identical machine IDs.

The standalone executable ignores inherited Python import paths. Its local
root comes from the public executable path, never PyInstaller's temporary
extraction directory or the caller's current directory. It bundles Python
application dependencies but continues to trust and invoke the host OpenSSH
and rsync commands.

## Commands And Sudo

Remote scripts are delivered to a fixed non-PTY shell. Local subprocesses use
argument vectors. Stdout and stderr are drained concurrently and captured only
to configured bounds; binary data is base64 encoded.

`sudo_exec` always uses `sudo -n -k`. It neither accepts a password nor relies
on a cached authentication timestamp. A matching NOPASSWD rule is required.
A broad NOPASSWD rule grants arbitrary root shell authority; prefer a narrow
remote account and command policy, and require client approval for sudo calls.

Remote output, filenames, and error tails are data returned to the MCP client,
not local commands. They can still contain prompt injection, so client policy
and human review remain necessary for state-changing operations. MCP framing
uses stdout and diagnostics use stderr. Passwords, PINs, identity paths, and
private keys are absent from tool schemas and operation metadata.

## Transfers And Cleanup

Rsync uses the existing mux transport. File bytes remain outside MCP messages.
Controlled partials are verified by SHA-256 before atomic publication.
Timeout, cancellation, disconnect, client EOF, SIGINT, and SIGTERM stop owned
processes and transfers without discovering or killing unrelated work.

## Public Errors

Public errors use stable identifiers and remove private repository paths. Raw
OpenSSH stderr is never returned or logged because it may contain host,
identity, proxy, or local-session details. See [MCP tools](tools.md#error-contract)
for the public identifiers.

The initial master's stderr is continuously drained into a small bounded tail
so OpenSSH and its prompt helpers cannot deadlock on a full pipe. Raw contents
are never logged or returned. Only recognized missing-interactive-session
patterns become one stable path-free error; unknown failures expose the exit
status, not the diagnostic text.

## Deliberate Limitations

- The server controls transport and local handling, not the semantics of an
  arbitrary remote shell script.
- Standard OpenSSH configuration remains trusted and may execute local helper
  programs.
- Other processes running as the same local user can access files that normal
  filesystem permissions allow; the project does not create a user sandbox.
- Transfer operation metadata and sessions are in memory and do not survive
  server restart.
- Server identity is the effective OpenSSH `HostName` and `Port`; different
  names or addresses for the same machine are distinct servers.
- Cleanup owns only processes and runtime paths created by this server; it does
  not discover or terminate unrelated SSH sessions.
