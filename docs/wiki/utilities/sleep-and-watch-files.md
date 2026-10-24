---
title: "Sleep and watch files"
summary: "Pause a turn for a set time or until a workspace file changes."
order: 2
tools: [workspace_ext__sleep, workspace_ext__watch_files]
related: [triggers/waiting-for-events, workspaces/files-and-log]
---
## What it is
`workspace_ext__sleep` pauses your turn for a fixed duration and `workspace_ext__watch_files` pauses it until a workspace path changes. Both are user-tier, yielding tools: the call parks the turn and releases the worker, and the turn resumes with the tool answer. Neither holds a worker while it waits.

## Concepts
- `workspace_ext__sleep` takes `seconds` (zero allowed, fractional allowed) and parks for that long. The answer is `{requested_seconds, elapsed_seconds}`; a cancel of the park adds `cancelled: true` and `cancel_reason`. A `seconds` of zero answers at once without parking. The wait is bounded by the global yield cap, 60 minutes by default: longer waits are not on today's tool surface.
- `workspace_ext__watch_files` takes `paths` (1 to 200 workspace-relative paths; files and directories, a directory watched recursively at one level, its child files' mtime changes count), an optional `timeout_seconds` and `batch_window_ms` (0 to 5000, default 250). Absolute paths and `..` segments are refused up front with `type=bad-request`. It parks until something under a watched path changes, then resumes with `{timed_out: false, changes: [...]}`; a deadline with no change resumes with `{timed_out: true, changes: [], elapsed_seconds: ...}`; a cancel of the park resumes with `{cancelled, ...}`. Each change carries `{path, event_type, mtime_after}`.
- `batch_window_ms` coalesces a burst: after the first change, the watcher waits that long for more before resuming once, so a compile that touches fifty files is one answer, not fifty.
- `timeout_seconds` omitted falls back to the global yield cap, the same 60 minute default that bounds `sleep`.
- The park is durable: the parked state is persisted, so a resume can run on a different worker process than the one that parked the turn.
- Both tools are invisible to outside MCP clients: MCP v1 has no park and resume, so yielding tools are not exposed there; an outside client polls the workspace instead (for example `workspaces__read_workspace_file`).
- `workspace_ext__watch_files` needs a workspace session: the watcher resolves the paths from the workspace's filesystem root, and a call without one is a `bad-request`. `sleep` needs no workspace.

## How to
1. Choose the wait that matches the event: a fixed delay for `sleep`, a path change for `watch_files`, a platform event for `system__wait_for_event` (triggers/waiting-for-events) and a person for `system__ask_user`.
2. For `watch_files`, name the exact files or the smallest directories that hold them, and set `timeout_seconds` when the change must not wait the full cap.
3. Raise `batch_window_ms` for noisy sources (a long compile, a large tree) so the burst lands as one resume.
4. Read the answer: continue on a change or an elapsed sleep, decide on `timed_out` whether to re-watch or proceed, and stop waiting on a cancel.

## Tool calls
### Sleep
`workspace_ext__sleep` parks the turn for the given seconds.

```json
{"toolset_id": "workspace_ext", "tool_name": "sleep", "arguments": {"seconds": 30}}
```

Reference: [`workspace_ext__sleep`](reference/tools/workspace_ext/sleep)

### Watch files
`workspace_ext__watch_files` parks the turn until a watched path changes or the timeout passes.

```json
{"toolset_id": "workspace_ext", "tool_name": "watch_files", "arguments": {"paths": ["out/report.md"], "timeout_seconds": 300, "batch_window_ms": 500}}
```

Reference: [`workspace_ext__watch_files`](reference/tools/workspace_ext/watch_files)

## Gotchas
- Use these for things a machine does; `system__ask_user` is the tool for a person, and `system__wait_for_event` is the tool for platform events (triggers/waiting-for-events).
- A watch of a path that changes every few seconds (a log under a chatty process) re-parks as fast as it resumes: watch the artifact you care about, not the stream.
- The changes answer reports what changed, not what the new content is: read the file after the resume (`workspaces/files-and-log`).
- Outside MCP clients cannot call these tools at all; for an outside poller, the workspace file read is the surface.

## Related
[Waiting for events](triggers/waiting-for-events), [files and log](workspaces/files-and-log).
