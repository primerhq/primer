---
title: "In-session file tools"
summary: "The seven file and shell tools every session has without asking: ls, read, write, edit, glob, grep and exec."
order: 6
tools: [workspace__ls, workspace__read, workspace__write, workspace__edit, workspace__glob, workspace__grep, workspace__exec]
related: [workspaces/files-and-log, workspaces/agents-md]
---
## What it is
Every session in a workspace has seven file and shell tools without a grant: `workspace__ls`, `workspace__read`, `workspace__write`, `workspace__edit`, `workspace__glob`, `workspace__grep` and `workspace__exec`. They are bound to the session's own workspace, so they take no `workspace_id`, and every path is relative to the workspace root.

## Concepts
- Call them directly by their scoped id: `{"name": "workspace__read", "arguments": {...}}`. `system__call_tool` cannot reach them.
- A path that resolves outside the workspace root is a `bad-request`.
- `workspace__ls` lists a folder, one entry per line with its kind, size, time and name. `show_hidden` adds dotfiles; `recursive` walks down, to `max_depth` if you set it. A recursive walk over a very large tree is capped, and a last line says the listing is partial.
- `workspace__read` returns numbered lines from `offset` (0-based), `limit` lines at a time (2000 by default). A binary file returns a summary instead. A read marks the file as read for this session.
- `workspace__write` creates or replaces a whole file. A new file is always allowed. An existing file is refused with `conflict` ("read it first or pass force=True") unless this session read it first, or wrote it itself, or you pass `force: true`. Use `force` only when you mean to discard what you have not seen.
- `workspace__edit` replaces `old_string` with `new_string`. `old_string` must occur exactly once unless you pass `replace_all: true`; a missing or repeated string is a `bad-request`, and so is a `new_string` equal to `old_string`.
- `workspace__glob` finds files by name pattern (`src/**/*.py`), newest first, `limit` (250 by default) at a time from `offset`.
- `workspace__grep` searches contents with a regular expression of at most 1000 characters. `output_mode` is `files_with_matches` (the default), `content` or `count`; `glob` filters the files; `case_insensitive`, `multiline` and `context` tune the match; `head_limit` caps the lines (250 by default). One call may spend 10 seconds matching in all, and one line search 2 seconds. A pattern that backtracks past either fails with an error that names the cause: simplify it, or narrow `path` or `glob`.
- `workspace__exec` runs a shell command and answers `<exit code>`, then stdout, then stderr. `command` and `description` (one line: what the command does) are required. `workdir` is the folder it runs in, `timeout_ms` bounds it (120000 by default), and `check: true` turns a non-zero exit into a failed call, which a graph needs to stop on a failing step. `background` must stay `false`.
- On a local provider the command gets only a few of the server's variables (`PATH`, `HOME`, `LANG` and the like) plus the workspace's `env`, never the server's other settings or keys.
- Write locking. `workspace__write`, `workspace__edit` and `workspace__exec` share one write lock per directory. Writers to the same directory serialize: each waits its turn. Writers to different directories run in parallel. `exec` takes two hints:
  - `access` is `write` (the default) or `read`. Declare `read` for a command that only reads (`cat`, `grep`, `ls`): it skips the lock and runs alongside writers. A wrong `read` is never worse than no hint, but it gives up the protection, so leave the default when unsure.
  - `writes` lists the paths or globs the command writes. The lock then covers those paths instead of the whole `workdir`, so more commands run in parallel.
- The scoping is best effort. A `write` to `a/f.txt` and an `exec` with `workdir` `a` serialize; an `exec` in `b` that writes `../a/f.txt` does not. Declare `writes` whenever a command writes outside its `workdir`. A template can set `strict_write_locking` to lock the whole workspace root instead.
- Stopping a command. When `timeout_ms` passes, or the call is stopped or cancelled, everything the command started (children, pipelines, background jobs) is killed, and only then is the lock released.
- Background jobs. A call returns when the command has finished and its output is closed. A job left running with the call's output (`server &`) keeps the output open, so the call waits until its timeout, fails, and the job is killed with the rest. `nohup` does not change that. A job whose output is redirected (`server >log 2>&1 &`) lets the call return at once and keeps running. To start something that must outlive a call that may time out or be stopped, detach it and redirect it: `setsid server >log 2>&1 &`.
- Large outputs. A result over 2000 lines or 50 KiB is saved to `.tmp/<sid>/` and you get a preview plus the file's path. Read the rest with `workspace__read` (an `offset` and a `limit`) or `workspace__grep`, not with `cat`.
- Stop and the file writers. A Stop while `workspace__write` or `workspace__edit` runs waits for it to finish and keeps its real result. A Stop during `workspace__exec` cancels it, and a cancelled command may have had effects before it stopped.

## How to
1. Look around with `workspace__ls`, `workspace__glob` and `workspace__grep`.
2. Read before you change: `workspace__read` the file, then `workspace__edit` a part of it or `workspace__write` the whole of it.
3. Run commands with `workspace__exec`. Give each one a `description`, a `timeout_ms` that fits, and `access: "read"` when it only reads.
4. Keep shared notes for other sessions under `.state/shared/`, and large results in files.

## Tool calls
### List a folder

```json
{"name": "workspace__ls", "arguments": {"path": "src"}}
```

Reference: [`workspace__ls`](reference/tools/workspace/ls)

### Read part of a file
Reads lines 101 to 150.

```json
{"name": "workspace__read", "arguments": {"path": "src/parser.py", "offset": 100, "limit": 50}}
```

Reference: [`workspace__read`](reference/tools/workspace/read)

### Write a file
Creates the file, or replaces one this session has read.

```json
{"name": "workspace__write", "arguments": {"path": ".state/shared/handoff.json", "content": "{\"next_action\": \"deploy\", \"branch\": \"main\"}\n"}}
```

Reference: [`workspace__write`](reference/tools/workspace/write)

### Edit a file
Replaces one unique string.

```json
{"name": "workspace__edit", "arguments": {"path": "src/parser.py", "old_string": "DEBUG = True", "new_string": "DEBUG = False"}}
```

Reference: [`workspace__edit`](reference/tools/workspace/edit)

### Find files by name

```json
{"name": "workspace__glob", "arguments": {"pattern": "**/*.py", "path": "src"}}
```

Reference: [`workspace__glob`](reference/tools/workspace/glob)

### Search contents
Shows each matching line with its path and line number.

```json
{"name": "workspace__grep", "arguments": {"pattern": "TODO", "output_mode": "content", "glob": "*.py"}}
```

Reference: [`workspace__grep`](reference/tools/workspace/grep)

### Run a command
Runs the tests, fails the call on a non-zero exit, and narrows the lock to the reports folder.

```json
{"name": "workspace__exec", "arguments": {"command": "pytest -q --junitxml=reports/junit.xml", "description": "run the test suite", "timeout_ms": 600000, "check": true, "writes": ["reports/"]}}
```

### Run a read-only command

```json
{"name": "workspace__exec", "arguments": {"command": "git status --short", "description": "show changed files", "access": "read"}}
```

Reference: [`workspace__exec`](reference/tools/workspace/exec)

## Gotchas
- These tools exist only inside a workspace session. From outside, use the `workspaces__*` file tools: [files and the log](workspaces/files-and-log).
- Without `check: true`, a failing command returns normally and its exit code is only in the text.
- `exec` output is decoded as UTF-8 with replacement characters; binary output is mangled. Write it to a file instead.
- Other sessions in the workspace use the same files and the same lock. Two sessions that write the same file race between their reads and writes.
- A daemon started with a plain `&` dies with the call. Use `setsid cmd >log 2>&1 &`.
- `agents.md` at the root is read by every agent in the workspace at every turn. Writing it changes their instructions: [agents.md](workspaces/agents-md).

## Related
[files and the log](workspaces/files-and-log), [agents.md](workspaces/agents-md).
