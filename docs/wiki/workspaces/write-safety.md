---
title: "Concurrent writes and command safety"
summary: "Use the workspace lock tiers and exec intent hints, and distinguish foreground commands from background jobs."
order: 11
related: [workspaces/in-session-file-tools, workspaces/files-and-log, workspaces/templates]
---

## What it is

Sessions share the same workspace filesystem. The locking and command-intent rules below govern concurrent access; a successful command is not proof that a later concurrent write preserved your earlier read.

## Concepts


The workspace serializes concurrent mutations so two writers never tear
each other's files. `write`, `edit`, and `exec` share one per-directory
write lock: writers to the SAME directory serialize (each waits its
turn), while writers to DIFFERENT directories run in parallel with no
contention.

`exec` takes two optional hints that tune this locking:

- `access: "read" | "write"` (default `"write"`) - declare `"read"` for
  a read-only command (`cat`, `grep`, `ls`, and similar) so it skips
  the write-lock entirely and runs fully parallel, even against other
  writers. Declaring `"read"` for a command that actually writes is
  never worse than not having this hint at all, but it does forfeit
  the protection; when unsure, leave the default `"write"`.
- `writes: [path, ...]` - when a command's write targets are known
  ahead of time, list them so the lock narrows to just those specific
  paths instead of the whole working directory, for more parallelism
  with other writers in the same directory.

This is best-effort scoping, not a hard guarantee across arbitrary
targets: a `write` to `a/f.txt` and an `exec` with `workdir="a"`
serialize because they share the same directory scope, but a `write`
to `a/f.txt` and an `exec` with `workdir="b"` do NOT serialize even if
that command's actual target is `../a/f.txt`. Declare `writes`
explicitly whenever a command's real write target lives outside its
`workdir`.

### Stopping a command, and background jobs

On a local workspace, `exec` runs the command in its own process
group. When it times out (`timeout_ms`) or is cancelled, everything
the command started (children, pipelines, background jobs) is killed
with it, and the write lock is released only after that.

A background job (`cmd &`) does not make the call return by itself:
the call returns when the command has finished AND its output is
closed. A job that inherited the call's output (no redirect) keeps it
open, so the call waits until `timeout_ms`, returns a timeout error,
and the job is killed with the rest of the group. `nohup` does not
change that: it only ignores the hangup signal, the job stays in the
group, and it is killed too. A job whose output is redirected
(`cmd >log 2>&1 &`) lets the call return at once, and nothing is
killed when a command finishes on its own, so such a job keeps running.

To start something that must outlive a call that may time out or be
cancelled (a dev server, a daemon), detach it into its own session AND
redirect its output: `setsid cmd >log 2>&1 &`. A process in its own
session is outside the group, so it is not killed.

## How to

Read the file, make the smallest intended write and choose the command's access and writes hints accurately. Serialize commands that mutate the same directory, and use an isolated directory when independent jobs can run safely. Inspect the actual exit code and output before reporting success.

## Tool calls

Use the worked commands on [in-session file tools](workspaces/in-session-file-tools). Their live reference is [`workspace__exec`](reference/tools/workspace/exec); external file writes use the separate [workspace file tools](workspaces/files-and-log).

## Gotchas

A read lock is not a write permission. The declared intent must describe the actual command, and the filesystem provider's enforcement limits still apply.

## Related

[In-session file tools](workspaces/in-session-file-tools), [files and log](workspaces/files-and-log), [templates](workspaces/templates).
