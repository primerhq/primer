---
slug: workspace-toolset
title: Workspace toolset
section: features
summary: "The workspace tools (ls, read, write, edit, glob, grep, exec) auto-registered with any agent that runs in a workspace session, plus the separate workspaces orchestration toolset."
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
These seven are the inside view: how an agent acts within the workspace it is running in. They are different from the reserved `workspaces` toolset (covered at the end of this page), which an agent binds explicitly to manage workspaces, templates, and sessions from the outside.
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

## The `workspaces` orchestration toolset

Everything above is the *inside* view. There is also a separate, reserved `workspaces` toolset that gives an agent the *outside* view: it manages workspace resources programmatically (provider and template CRUD, workspace materialisation, session lifecycle, remote file I/O, and the state-repo log). Unlike the seven tools above, it is **not** auto-registered: an agent binds it explicitly on its Tools tab, the same way it binds any other toolset. Its scoped ids carry the `workspaces__` prefix.

Agents bind it to act as meta-agents: spawn a workspace, start a child session inside it, steer that session mid-run, read its output files, and tear it down when finished.

It exposes these tool groups:

| Group | Tools | What it covers |
|---|---|---|
| Providers | 4 | Register, list, get, and remove backend configurations |
| Templates | 5 | Define and manage materialisation recipes |
| Workspaces | 4 | Materialise, inspect, and destroy workspaces |
| Sessions | 7 | Create, control, steer, and inspect workspace sessions |
| Files | 5 | Read, write, list, inspect, and delete files in a workspace by id |
| Log | 1 | Fetch workspace state-repo history |

```callout:note
The workspace-session **yielding** tools (`sleep`, `watch_files`, `invoke_graph`, `subscribe_to_trigger`) no longer live in the `workspaces` toolset. They moved to the separate reserved `workspace_ext` toolset (scoped ids `workspace_ext__watch_files`, `workspace_ext__invoke_graph`, and so on). Like `workspaces`, `workspace_ext` is bound explicitly on an agent's Tools tab, but its tools are registered only when the agent runs in a workspace session and are suppressed when the agent is invoked on a chat. See the Yielding tools page.
```

```callout:note
The `workspaces` file tools (`list_workspace_files`, `read_workspace_file`, `write_workspace_file`, ...) act on any workspace by id from the outside; they are distinct from the seven `ls`/`read`/`write`/... tools above, which act on the agent's own workspace. Bind the `workspaces` toolset only when an agent needs to manage workspaces other than the one it runs in.
```

### Walkthrough: spawn a child session and read its output

1. Give your orchestrator agent the **workspaces** toolset on its Tools tab.
2. Start a session with the orchestrator agent. In the initial instructions, give it a task like "Create a session for agent code-reviewer on workspace ws-1, wait for it to finish, and read the output file report.md."
3. The agent calls `workspaces__create_workspace_session` with `workspace_id="ws-1"`, `binding={"kind": "agent", "agent_id": "code-reviewer"}`, and `auto_start=true`.
4. It polls `workspaces__get_workspace_session` until `status` reaches `ended`.
5. It calls `workspaces__read_workspace_file` with `workspace_id="ws-1"` and `path="report.md"` to retrieve the result.

```embed:agents-page
```

### Session tools

A session binds a workspace to either an agent or a graph. `workspaces__create_workspace_session` starts one with these fields:

| Field | Description |
|---|---|
| `workspace_id` | The workspace the session runs in. |
| `binding` | `{"kind": "agent", "agent_id": "..."}` or `{"kind": "graph", "graph_id": "..."}`. |
| `initial_instructions` | Optional instructions injected before the first turn. |
| `auto_start` | Default `true`. When `false` the session is created in `CREATED` status and waits for an explicit resume. |
| `graph_input` | Required when `binding.kind == "graph"`; passed as the graph input. |
| `parent_session_id` | Optional; links this session to a parent for graph hierarchy tracking. |

The rest control a live session: `cancel_workspace_session` (hard-cancel), `pause_workspace_session` / `resume_workspace_session` (operator-level halt between turns), `steer_workspace_session` (append a steering instruction the agent sees on its next turn), `list_workspace_sessions`, and `get_workspace_session` (returns `{info, status}`).

### Provider, template, and workspace tools

These mirror the console's Workspaces UI: `create_workspace_provider` / `list_workspace_providers` / `get_workspace_provider` / `delete_workspace_provider`; `create_workspace_template` / `update_workspace_template` / the matching list/get/delete; and `create_workspace` / `delete_workspace` / `list_workspaces` / `get_workspace`. See the workspace providers and templates pages for the field shapes.

### Remote file and log tools

`list_workspace_files`, `get_workspace_file_info`, `read_workspace_file`, `write_workspace_file`, and `delete_workspace_file` operate on a workspace named by `workspace_id` (text or base64 content), refusing paths inside the reserved `.state/` and `.tmp/` trees. `get_workspace_log` fetches recent commits from the workspace's `.state` git repository, each carrying parsed `X-Primer-*` trailers (workspace, session, agent, op, tool, call).

```ref:features/workspace-providers
Workspace backends (local, container, kubernetes) and how a workspace is materialised.
```

```ref:features/sessions
Workspace sessions: how an agent run is bound to a workspace.
```

```ref:features/yielding-tools
watch_files, invoke_graph, and the park-resume protocol.
```
