# Security Model

Remote SSH MCP gives an agent the authority of a local user and one remote SSH
account. It narrows connection and file-handling behavior; it is not a sandbox
for arbitrary remote commands.

## Trust Boundaries

Trusted inputs:

- the local user and files writable by that user;
- the selected SSH account and remote programs it executes;
- system and user OpenSSH configuration;
- MCP client approval policy.

Agent-controlled inputs include commands, remote paths, relative local paths,
transfer overwrite decisions, connection aliases, and direct host/user/port
values. These are validated, bounded, or quoted at their local process
boundary. Remote file contents and filenames remain untrusted data.

OpenSSH configuration is executable configuration. `ProxyCommand`,
`ProxyJump`, `Match exec`, token providers, and similar directives can invoke
local programs. Review configuration before exposing a matching alias or host
to an agent.

## SSH Isolation

- MCP startup performs no authentication; only `connect` opens SSH.
- The server owns one foreground `ControlMaster=yes`, `ControlPersist=no`
  process and a private control socket.
- Mux clients disable authentication and use a failing proxy command. If the
  socket disappears, they cannot fall back to a fresh login.
- Automatic reconnect is absent, preventing surprise PIN dialogs or additional
  hardware-key touches after transport loss.
- Agent, X11, local, remote, and dynamic forwarding are cleared or disabled.
  SSH-configured local and remote commands are disabled.
- Normal OpenSSH host-key verification, proxies, identity selection, and
  FIDO2/PIN integration remain active for the initial master.

## Local Filesystem

`--local-root` must resolve to an existing absolute directory owned by the
current user and not writable by group or others. Agent inputs are relative
paths. Traversal, protected internal paths, NULs, and escapes through symlinks
are rejected.

The internal spool and partial directories are real owned directories with
mode `0700`. Output files use restrictive creation modes. Downloads verify a
controlled partial before atomic publication; overwrite is explicit. These
rules assume other processes running as the same local user are trusted.

## Commands, Output, And Prompt Injection

Remote shell syntax is allowed only in the explicit command payload. Local
subprocesses use argument vectors and fixed resolved executables. Connection
data never becomes shell syntax.

Captured output is bounded and binary-aware. Remote text, filenames, and error
tails are data returned to the MCP client; they are not interpreted as local
commands. As with any agent tool, remote content can contain prompt injection,
so client policy and human review remain necessary for state-changing actions.

Internal exceptions and sensitive subprocess details are reduced to stable
public errors. MCP framing uses stdout; diagnostics use stderr. Passwords,
PINs, identity paths, and private keys are neither accepted by tool schemas nor
written to operation metadata.

## Sudo

`sudo_exec` allocates no PTY and uses `sudo -n -k`. `-n` forbids prompting;
`-k` makes the command ignore cached human authentication. A matching NOPASSWD
rule is required. The server never accepts or injects a sudo password.

A broad NOPASSWD rule gives the agent arbitrary root shell authority. Prefer a
narrow remote account and narrowly scoped policy where practical, and require
client approval for every `sudo_exec` call.

## Transfers And Cleanup

Rsync receives the same mux-only SSH transport as commands. Transfer streams
are bounded to progress and diagnostic tails; file bytes remain outside model
context. Controlled partials reject unsafe types and are verified with SHA-256
before publication.

Timeout, cancellation, disconnect, client EOF, SIGINT, and SIGTERM terminate
owned process groups, close the owned master, and remove private runtime state.
Cleanup does not discover, adopt, or terminate unrelated SSH masters.

## Deliberate Limitations

- one active target per server process;
- no PTY, interactive shell, password authentication, or sudo passwords;
- no automatic reconnect or unattended reauthentication;
- no tunnel management or remote MCP network service;
- no recursive sync, cross-remote transfer, or additional remote path sandbox;
- single-file transfers target POSIX-like remote systems.

Use an ordinary SSH client for workflows that genuinely require a PTY. Raw
terminal streams are harder for agents to parse, bound, recover, and use for
large files, so structured MCP tools should remain the default.
