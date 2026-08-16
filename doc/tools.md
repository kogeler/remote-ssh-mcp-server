# MCP Tools

Every input model is strict and rejects unknown fields. Responses contain an
`ok` flag and either structured `result` data or a stable public `error`.

| Tool | Purpose |
| --- | --- |
| `connect` | Open one SSH master using an alias or host/user/port. |
| `disconnect` | Cancel active work and close the owned master. |
| `connection_status` | Report lifecycle state without opening SSH. |
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

## Connection Lifecycle

`connection_status` reports `disconnected`, `starting`, `ready`, or `lost`.
Only `connect` can authenticate. One server owns at most one target and one
master, and `disconnect` is idempotent. After master loss, operational tools
return `connection_lost`; the caller must disconnect before an explicitly
approved reconnect.

## Commands

`exec` and `sudo_exec` run independent non-interactive shells. A command's
working directory, variables, aliases, and other shell state do not persist.
The optional `cwd` applies only to the current invocation.

Results separate stdout and stderr and include exit code, duration, timeout,
captured bytes, total bytes, and truncation state. UTF-8 data is returned as
text; other bytes are base64 encoded. Capture is limited by
`--max-output-bytes`. Set `spool_output=true` only when complete streams must be
written under the protected local root.

`sudo_exec` always starts sudo with non-interactive and timestamp-invalidation
semantics: `sudo -n -k`. It succeeds only when sudoers allows the Bash command
with NOPASSWD. Password-required, policy-denied, missing-sudo, and post-start
command failures are distinct outcomes.

## Inspection

Inspection tools quote remote paths and return structured metadata rather than
parsing terminal layout. Directory entries preserve unusual byte sequences by
returning UTF-8 or base64-encoded names. Range reads never return more than the
requested bound or the configured output limit.

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
is limited by `--max-transfers`, and each active destination is exclusively
owned by one operation.

## Error Contract

Public errors use stable identifiers such as:

- `not_connected`, `already_connected`, `disconnect_required`
- `connection_start_failed`, `connection_lost`
- `invalid_arguments`, `invalid_command`, `invalid_local_path`
- `local_path_exists`, `remote_path_exists`, `remote_path_not_found`
- `sudo_unavailable`, `sudo_password_required`, `sudo_not_allowed`
- `transfer_not_found`, `transfer_failed`, `verification_failed`

Human-readable messages provide context, but automation should use the error
identifier rather than localized OpenSSH, rsync, or sudo text.
