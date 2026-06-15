---
slug: workspace-toolset
title: Workspace Toolset
section: workspaces
summary: "The seven workspace tools (ls, read, write, edit, glob, grep, exec) auto-registered with any agent that runs inside a workspace session."
---

## What the workspace toolset is

When an agent runs inside a workspace session, primer automatically registers a fixed set of workspace tools with it. You do not add these on the agent's Tools tab and you cannot remove them: any agent that executes in a workspace gets them, because their whole purpose is to grant that agent access to its workspace.

These tools operate on the **current session's workspace**, its filesystem and a shell. They are the same tools whether the workspace runs on the local filesystem, a container, or a Kubernetes pod: the backend differs but the tool surface is identical.

There are seven of them, and they use short, conventional names (no `workspace` prefix):

| Tool | What it does |
|---|---|
| `ls` | List a directory |
| `read` | Read a file with line offset/limit paging |
| `write` | Create or replace a file |
| `edit` | Replace a substring in a file |
| `glob` | Find files by glob pattern |
| `grep` | Search file contents by regex |
| `exec` | Run a shell command |

```callout:note
These seven are the inside view: how an agent acts within the workspace it is running in. They are distinct from the reserved `workspaces` orchestration toolset, which an agent binds explicitly to manage workspaces, templates, and sessions from the outside. That toolset is covered alongside the entities it operates on; see the workspace providers, templates, and sessions pages.
```

## The seven workspace tools

### `ls`

Lists a directory inside the workspace.

- `path` (default `.`): directory relative to the workspace root.
- `show_hidden` (default false): include dotfiles.
- `recursive` (default false) and `max_depth`: walk subdirectories, optionally bounded.

Output is one line per entry as `<type> <size> <name>`, where type is `f`, `d`, or `l` (file, directory, symlink), sorted alphabetically.

### `read`

Reads a file with offset/limit paging, the same shape coding assistants use, so large or truncated files can be read in pages.

- `path` (required): file relative to the workspace root.
- `offset` (default 0): line number to start from.
- `limit` (default 2000): maximum lines to return.

Output is the requested lines prefixed with line numbers. Binary files return a stable summary (`<binary file: ...>`) rather than raw bytes.

### `write`

Creates or replaces a file.

- `path` (required), `content` (required), `mode` (optional octal, default 0644).
- `force` (default false): bypass the read-before-write guard.

`write` enforces a **read-before-write** rule: it refuses to overwrite an existing file the agent has not `read` during the current session, unless `force=true` is passed. Creating a new file is always allowed. This mirrors the safety rule coding assistants use to avoid clobbering content the agent has not actually seen.

### `edit`

A targeted string-replace edit, the workhorse for incremental changes.

- `path` (required), `old_string` (required, the exact substring to replace), `new_string` (required).
- `replace_all` (default false): replace every occurrence; otherwise `old_string` must be unique in the file.

It errors clearly when `old_string` is not found, or is non-unique without `replace_all`.

### `glob`

Finds files by glob pattern.

- `pattern` (required, e.g. `src/**/*.py`), `path` (default `.`), plus `limit` and `offset` for paging.

Returns matching paths, newest first.

### `grep`

Searches file contents by regular expression (uses ripgrep when available, with a Python fallback).

- `pattern` (required regex), `path` (default `.`), and `glob` to filter which files are searched.
- `output_mode`: `files_with_matches` (default), `content`, or `count`.
- `case_insensitive`, `multiline`, `context` (lines of context around a match), and `head_limit`.

In `content` mode it emits `<path>:<lineno>:<text>`; in `count` mode, `<path>:<count>`.

### `exec`

Runs a shell command in the workspace. This is what lets an agent build, test, run scripts, use git, install packages, and generally do real work, not just file edits.

- `command` (required): a command line passed to a shell.
- `workdir` (default `.`): working directory relative to the workspace root.
- `timeout_ms` (default 120000): a hard timeout.
- `background` (default false): return immediately with a process handle instead of blocking.
- `description` (required): a one-line description of what the command does.

In the foreground it returns the exit code, then stdout, then stderr (truncated by the standard output policy). In the background it returns a process id and an output path; the agent reads the streaming output through the regular `read` tool, and backgrounded processes are reaped when the workspace closes.

```callout:warning
`exec` gives the agent a real shell inside the workspace sandbox. Scope what a workspace agent can reach through its workspace template and the backend you run it on (a throwaway local directory, a container, or a Kubernetes pod), and gate sensitive operations with an approval policy if needed.
```

```ref:workspaces/workspace-providers
Workspace backends (local, container, kubernetes) and how a workspace is materialised.
```

```ref:workspaces/workspaces-and-sessions
Workspace sessions: how an agent run is bound to a workspace.
```

```ref:workspaces/yielding-tools
watch_files, invoke_graph, and the park-resume protocol.
```
