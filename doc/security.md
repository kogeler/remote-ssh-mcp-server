# Security Model

Remote SSH MCP gives an MCP client the authority of one local user and one
remote SSH account. It narrows transport, capture, filesystem, and cleanup
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
- `connect` owns one foreground OpenSSH ControlMaster and a private socket.
- Mux clients disable passwords, public-key authentication, host-based
  authentication, GSSAPI, and fallback proxying.
- Forwarding, agent sharing, X11 forwarding, and SSH-configured local and
  remote commands are disabled.
- Master loss is reported and never starts another authentication.
- Linux session recovery may import only the documented eight routing values;
  non-empty inherited values win. It never imports `PATH`, `HOME`, loader or
  Python variables, credentials, or an arbitrary login environment.
- Recovery is private to the first master subprocess and never changes
  `os.environ` or reaches commands and transfers.

Native OpenSSH remains responsible for host keys, proxies, identities, and
hardware-token prompts. The published `ssh-wrapper` library owns that
transport implementation; this project owns its MCP exposure.

## Local Files

Every model-selected local path is relative to one explicit local root: the
source project which owns the prepared venv, or the directory containing a
standalone executable. Traversal, NULs, protected internal paths, and symlink escapes are
rejected. Spools and transfer partials use private directories and restrictive
creation modes. Normal access controls of the current local user remain the
outer trust boundary.

The launcher validates and starts the prepared runtime with Python isolated
mode, so inherited `PYTHONPATH` and user-site packages cannot replace locked
dependencies. The installed module verifies that its active virtual
environment is a direct child of the marked MCP project, then uses that parent
as the local filesystem boundary. The project's `.version` must match the
active package. It never treats `site-packages`, the caller's working
directory, or an ambient import path as that boundary.

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
- Transfer operation metadata is in memory and does not survive server restart.
- Cleanup owns only processes and runtime paths created by this server; it does
  not discover or terminate unrelated SSH sessions.
