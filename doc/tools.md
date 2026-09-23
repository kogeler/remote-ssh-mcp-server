# MCP Tools

Every input model is strict and rejects unknown fields. Responses contain an
`ok` flag and either structured `result` data or a stable public `error`.

| Tool | Purpose |
| --- | --- |
| `connect` | Open a new independent session; returns its ID and one-time key. |
| `disconnect` | Close any session by ID without its key, cancelling its work. |
| `session_list` | List every session without keys and without opening SSH. |
| `exec` | Run one bounded non-PTY shell command. |
| `sudo_exec` | Run one command through passwordless-only sudo. |
| `stat` | Inspect one remote path. |
| `list_directory` | List bounded machine-readable directory metadata. |
| `read_file_range` | Read a bounded byte range from a regular file. |
| `download_start` | Start a verified background download. |
| `upload_start` | Start a verified background upload without sudo. |
| `transfer_status` | Poll one operation by ID. |
| `transfer_cancel` | Cancel a transfer and retain its resumable partial. |
| `transfer_list` | List retained transfer operations. |

## Sessions

One server process can hold several independent SSH sessions, so several
agents, or one agent working with several remote servers, can share it. Each
session owns exactly one OpenSSH master. Only `connect` can authenticate.
Direct mode requires `host` and `user`; its optional `port` defaults to `22`.

A successful `connect` returns the session's public `session_id` and a secret
`session_key`. The key is returned exactly once; no tool can return it again.
Every tool other than `connect`, `disconnect`, and `session_list` requires both
`session_id` and `session_key`, so an operation always names the one session,
and therefore the one remote server, that it targets. A key from another
session is rejected with `invalid_session_key`. An agent that loses its key
disconnects that session and connects again.

At most one session may own a remote server. Before authentication, `connect`
evaluates the target with `ssh -G` and identifies the server by its effective
OpenSSH `HostName` (case-insensitive) and `Port`; the SSH user is not part of
that identity. An alias and a direct host/user pair that resolve to the same
`HostName` and `Port` are the same server. Different names or addresses of the
same machine are not unified because DNS is not resolved. A second `connect`
to an owned server fails with `already_connected` and names the existing
session ID, including while that session is still `starting` or already
`lost`. Reopening requires disconnecting the existing session first.

`session_list` is visible to every caller and never includes keys. Each entry
reports the session ID, state (`starting`, `ready`, `lost`, or `closing`), the
requested target, the resolved `server_host` and `server_port`, the master PID,
creation and last-use times, and the number of active commands and transfers.
`disconnect` needs only `session_id`, so any caller can close any session,
including one that is still authenticating or has lost its master; its result
reports the final `closed` state. Closing an unknown or already closed ID
returns `session_not_found`.

Authentication prompts are serialized: one session authenticates at a time, so
each PIN dialog or hardware-key touch belongs to exactly one pending `connect`.
Ready sessions then work in parallel. `--max-sessions` bounds how many sessions
may exist, including sessions that are still authenticating. After master loss,
that session's operational tools return `connection_lost`; it never
reconnects, and only a disconnect followed by an explicitly approved `connect`
authenticates again.

## Commands

`exec` and `sudo_exec` run independent non-interactive shells. `exec` uses
`/bin/sh`; `sudo_exec` uses a fixed `/bin/bash --noprofile --norc -s` program.
A command's working directory, variables, aliases, and other shell state do not
persist. The optional `cwd` applies only to the current invocation. A command
is limited to 1,048,576 UTF-8 bytes; an explicit timeout must be between 0.1
and 86,400 seconds.

Results separate stdout and stderr and include exit code, duration, timeout,
captured bytes, total bytes, and truncation state. UTF-8 data is returned as
text; other bytes are base64 encoded. Capture is limited by
`--max-output-bytes`. Set `spool_output=true` only when complete streams must be
written under the local root's private internal directory.

`sudo_exec` always starts sudo with non-interactive and timestamp-invalidation
semantics: `sudo -n -k`. It succeeds only when sudoers allows the Bash command
with NOPASSWD. Password-required, policy-denied, missing-sudo, and post-start
command failures are distinct outcomes.

## Inspection

Inspection tools quote remote paths and return structured metadata rather than
parsing terminal layout. Directory entries preserve unusual byte sequences by
returning UTF-8 or base64-encoded names. Range reads never return more than the
requested bound or the configured output limit. `list_directory` rejects a
non-directory source, and `read_file_range` rejects a non-regular-file source.
Its `eof` flag is true when the returned range reaches the file's end,
including an exact-length final range.

## Large File Transfers

Large file bytes do not pass through MCP messages:

1. Start `download_start` or `upload_start`.
2. Save its `operation_id`.
3. Poll `transfer_status` until `completed`, `failed`, or `cancelled`.
4. Cancel only when the active copy must stop.

Rsync runs in the background over the existing SSH master. Deterministic
partial names allow the same source/destination pair to resume. The source and
partial are compared with SHA-256 before final publication. Downloads publish
atomically inside the local root; uploads publish through a same-directory
remote rename or link. Existing final paths require `overwrite=true`.
If a no-overwrite upload destination appears after the initial check, the
atomic link fails, its now-useless remote partial is removed, and the operation
reports `remote_path_exists` only after that cleanup finishes.

Transfers are single-file operations. They never use sudo. Active concurrency
is limited per session by `--max-transfers`. Each active destination is
exclusively owned by one operation across all sessions: a local download path,
or a remote upload path of the same SSH authority. An `operation_id` is visible
only through the session that started it.

## Error Contract

Public errors use stable identifiers such as:

- `session_not_found`, `invalid_session_key`, `session_not_ready`,
  `session_closing`, `session_closed`
- `already_connected`, `session_limit_reached`, `server_closing`
- `connection_start_failed`, `connection_lost`
- `invalid_arguments`, `invalid_command`, `invalid_local_path`, `invalid_remote_type`
- `local_path_exists`, `remote_path_exists`, `remote_path_not_found`
- `sudo_unavailable`, `sudo_password_required`, `sudo_not_allowed`
- `transfer_not_found`, `transfer_conflict`, `transfer_limit_reached`,
  `transfer_failed`, `verification_failed`

Human-readable messages provide context, but automation should use the error
identifier rather than localized OpenSSH, rsync, or sudo text.
An `invalid_arguments` message names the missing or invalid schema fields, such
as `session_id` and `session_key`, and states when unknown fields were sent. It
never repeats argument values or the names of unknown fields.
The initial master's raw stderr is never public. A recognized missing
interactive askpass route uses the stable message `SSH authentication requires
an interactive system prompt, but no usable user session environment was
available`; unknown early exits retain a concise status-only message.
