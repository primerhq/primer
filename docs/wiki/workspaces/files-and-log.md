---
title: "Files and the log"
summary: "Read, write, list and delete files in any workspace from outside its sessions, and read the workspace log."
order: 3
tools: [workspaces__list_workspace_files, workspaces__get_workspace_file_info, workspaces__read_workspace_file, workspaces__write_workspace_file, workspaces__delete_workspace_file, workspaces__get_workspace_log]
related: [workspaces/in-session-file-tools, workspaces/what-a-workspace-is]
---
## What it is
Six `workspaces__*` tools reach a workspace's files from outside its sessions: you name the `workspace_id` and a path relative to the workspace root. One of them, `workspaces__get_workspace_log`, reads the history of the workspace's `.state` repository.

## Concepts
- Every call takes a `workspace_id`. Inside a session, prefer the [in-session file tools](workspaces/in-session-file-tools): they are bound to your own workspace, page large files and track what you have read.
- The reach rule: from a session outside prime you reach only your own workspace, and any other id answers `not-found`, exactly like one that does not exist. From prime, or with no calling session, you reach every workspace.
- `workspaces__list_workspace_files` lists `path` (the root by default), one level or the whole tree with `recursive: true`, in pages of `limit` (200 by default, 2000 at most) from `offset`. Each item is a file entry: `path`, `kind` (`file`, `dir` or `symlink`), `size_bytes` and `modified_at`. On a very large tree the walk is capped: the answer then carries `truncated: true` and a `note`, and `total` is a lower bound, not the true size.
- `workspaces__get_workspace_file_info` answers one path's file entry.
- `workspaces__read_workspace_file` answers `{path, encoding, content, size_bytes}`. `encoding` is `text` (UTF-8, the default) or `base64` (for binary files). A file that is not valid UTF-8 read as `text` is a `bad-request` that tells you to ask for `base64`.
- `workspaces__write_workspace_file` replaces the file, or creates it with its parent folders. `encoding` works as for a read. There is no read-before-write check here: the write lands whatever the file held.
- `workspaces__delete_workspace_file` deletes one file or one empty folder. The workspace root cannot be deleted.
- The runtime's own trees, `.state/` and `.tmp/`, are judged by where a path resolves, so `./.state/x` and `a/../.state/x` count too:
  - Listing, inspecting or reading inside them needs an admin; anyone else gets `forbidden`, and a recursive listing simply leaves those entries out. A run is judged by who started it; a call over MCP, by its caller.
  - Writing or deleting inside them is refused for everyone (a delete answers `bad-request`).
  - To read a session's transcript, use `workspaces__read_workspace_session_messages`, not the raw file under `.state/sessions/`.
- `workspaces__get_workspace_log` answers `{commits: [...]}`: up to `limit` commits (50 by default, 500 at most), newest first. Each commit has `sha`, `subject`, `committed_at` and the parsed trailers: `workspace_id`, `session_id`, `agent_id`, `op`, `tool` and `call_id`. Use it to see which session and which tool call changed the workspace's state.

## How to
1. Find the file: list the folder you care about. On a big tree, list a subfolder with `recursive: true`, not the root.
2. Read it, with `base64` when it is not text.
3. To change it, read it just before you write, change the content, and write the whole file back.
4. To audit what sessions did, read the log and match `session_id` and `tool` against the sessions you know.

## Tool calls
### List files
Walks one folder and everything under it.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_files", "arguments": {"workspace_id": "invoice-parser", "path": "src", "recursive": true}}
```

Reference: [`workspaces__list_workspace_files`](reference/tools/workspaces/list_workspace_files)

### Inspect one path
Answers the kind, size and modification time of one path.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_file_info", "arguments": {"workspace_id": "invoice-parser", "path": "src/main.py"}}
```

Reference: [`workspaces__get_workspace_file_info`](reference/tools/workspaces/get_workspace_file_info)

### Read a file
Reads a text file; pass `"encoding": "base64"` for a binary one.

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_file", "arguments": {"workspace_id": "invoice-parser", "path": "README.md"}}
```

Reference: [`workspaces__read_workspace_file`](reference/tools/workspaces/read_workspace_file)

### Write a file
Replaces the file, or creates it, and answers `{path, size_bytes}`.

```json
{"toolset_id": "workspaces", "tool_name": "write_workspace_file", "arguments": {"workspace_id": "invoice-parser", "path": "notes/plan.md", "content": "1. Parse the March invoices.\n2. Report the totals.\n"}}
```

Reference: [`workspaces__write_workspace_file`](reference/tools/workspaces/write_workspace_file)

### Delete a file
Deletes one file, or one empty folder, and answers `{deleted: true, path}`.

```json
{"toolset_id": "workspaces", "tool_name": "delete_workspace_file", "arguments": {"workspace_id": "invoice-parser", "path": "notes/plan.md"}}
```

Reference: [`workspaces__delete_workspace_file`](reference/tools/workspaces/delete_workspace_file)

### Read the log
Answers the ten newest commits of the `.state` repository.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_log", "arguments": {"workspace_id": "invoice-parser", "limit": 10}}
```

Reference: [`workspaces__get_workspace_log`](reference/tools/workspaces/get_workspace_log)

## Gotchas
- `workspaces__write_workspace_file` overwrites without a check. A session in the workspace may have changed the file since you read it: read it again just before you write.
- `content` is the whole file. There is no append and no partial edit from outside a session.
- An `encoding` other than `text` or `base64` is a `bad-request`.
- A path that escapes the workspace root is a `bad-request`.
- From outside, you cannot write under `.state/shared/`. A session coordinates there with its own `workspace__write`.
- The log covers the `.state` repository only: the platform's commits of session state, not your project's own git history.

## Related
[in-session file tools](workspaces/in-session-file-tools), [what a workspace is](workspaces/what-a-workspace-is).
