---
slug: workspace-toolset
title: Workspace toolset
section: features
summary: The workspaces toolset -- 27 tools an agent uses to manage providers, templates, workspaces, sessions, files, and logs from inside a session.
---

## What the workspace toolset is

The `workspaces` toolset is a built-in, always-available toolset that gives agents programmatic access to every workspace resource: provider and template CRUD, workspace materialisation, session lifecycle, file I/O, and the state-repo log. It is the counterpart to the console's Workspaces UI.

Agents use it to act headlessly: spawn a workspace, start a child session inside it, steer that session mid-run, read its output files, and tear it down when finished. This is the pattern behind meta-agents that orchestrate other agents.

The toolset exposes 27 tools organised into five groups, each prefixed `workspaces__`:

| Group | Tool count | What it covers |
|---|---|---|
| Providers | 4 | Register and remove backend configurations |
| Templates | 5 | Define materialisation recipes |
| Workspaces | 4 | Materialise, inspect, and destroy workspaces |
| Sessions | 7 | Create, control, and inspect workspace sessions |
| Files | 5 | Read, write, list, inspect, and delete files |
| Log | 1 | Fetch workspace state-repo history |
| Yielding | 2 | watch_files (parks), invoke_graph (parks on HITL) |

The two yielding tools (`watch_files`, `invoke_graph`) require a workspace session context and are excluded from the MCP surface.

```ref:features/workspace-providers
The console UI for workspace providers and templates, plus which backends are available.
```

```ref:features/sessions
The session lifecycle that workspace sessions follow.
```

## Configuration

To make the workspace toolset available to an agent:

1. Open **Agents** and select the agent.
2. Go to the **Tools** tab.
3. Click **Add toolset** and select **workspaces**.
4. Save the agent.

The full toolset is enabled by default. To limit the agent to specific tools, select individual tools rather than the full toolset.

```embed:toolsets
```

## Walkthrough -- spawn a child session and read its output

1. Create a workspace using the console or via `workspaces__create_workspace` from another agent session.
2. Give your orchestrator agent the **workspaces** toolset.
3. Start a session with the orchestrator agent. In the initial instructions, give it a task like "Create a session for agent code-reviewer on workspace ws-1, wait for it to finish, and read the output file report.md."
4. The agent calls `workspaces__create_workspace_session` with `workspace_id="ws-1"`, `binding={"kind": "agent", "agent_id": "code-reviewer"}`, and `auto_start=true`.
5. The agent polls with `workspaces__get_workspace_session` until `status` reaches `ended`.
6. The agent calls `workspaces__read_workspace_file` with `workspace_id="ws-1"` and `path="report.md"` to retrieve the result.

```embed:workspaces
```

```embed:session-detail
```

## Tool reference

### Provider tools

These tools manage `WorkspaceProvider` rows -- the persisted backend configuration entries. The Update operation is intentionally absent; to change a provider configuration, delete and recreate.

**`workspaces__list_workspace_providers`** -- List configured providers with pagination. Returns `items`, `length`, `total` (offset mode), and `next_cursor` (cursor mode).

**`workspaces__get_workspace_provider`** -- Fetch one provider by id. Returns `type=not-found` when missing.

**`workspaces__create_workspace_provider`** -- Register a new backend. Body shape is the full `WorkspaceProvider` schema with a `provider` discriminator (`local`, `container`, or `kubernetes`) and matching `config`. Example:

```json
{
  "entity": {
    "id": "local-1",
    "provider": "local",
    "config": {"kind": "local"}
  }
}
```

**`workspaces__delete_workspace_provider`** -- Remove a provider by id. Cascades to drop its cached backend instance from the registry.

### Template tools

Templates are the materialisation recipes. A template references a `provider_id` and carries backend config (image, resources, env vars, init commands, initial files).

**`workspaces__list_workspace_templates`** -- List templates with pagination.

**`workspaces__get_workspace_template`** -- Fetch one template by id.

**`workspaces__create_workspace_template`** -- Create a new recipe. Body must reference an existing `provider_id`. Example:

```json
{
  "entity": {
    "id": "py-base",
    "description": "Python base image",
    "provider_id": "local-1",
    "backend": {"kind": "local"}
  }
}
```

**`workspaces__update_workspace_template`** -- Replace an existing template in place. The body `id` must match the path `id`. Existing materialised workspaces are NOT re-materialised; only future creates see the updated recipe.

**`workspaces__delete_workspace_template`** -- Remove a template. Existing workspaces that referenced it keep their snapshot `template_id` but the row no longer resolves.

### Workspace tools

These tools materialise and destroy live workspaces.

**`workspaces__list_workspaces`** -- List persisted workspace rows with pagination.

**`workspaces__get_workspace`** -- Fetch one workspace row by id.

**`workspaces__create_workspace`** -- Materialise a new workspace from a template. Looks up the template, calls the matching backend to create the live instance, and persists a `Workspace` row. Optional `overrides` layer per-instantiation env vars, files, and init commands on top of the template. Example:

```json
{
  "template_id": "py-base",
  "overrides": {"env": {"DEBUG": "1"}}
}
```

**`workspaces__delete_workspace`** -- Destroy a workspace, freeing both the backend resources and the persisted row. Not for retiring the template (use `delete_workspace_template`).

### Session tools

These tools start, control, inspect, and stop workspace sessions. A session binds a workspace to either an agent or a graph.

**`workspaces__create_workspace_session`** -- Start a session. Required fields:

| Field | Description |
|---|---|
| `workspace_id` | The workspace the session runs in. |
| `binding` | `{"kind": "agent", "agent_id": "..."}` or `{"kind": "graph", "graph_id": "..."}`. |
| `initial_instructions` | Optional instructions injected before the first turn. |
| `auto_start` | Default `true`. When `false` the session is created in `CREATED` status and waits for an explicit resume. |
| `graph_input` | Required when `binding.kind == "graph"`. Passed as the graph's input. |
| `parent_session_id` | Optional; links this session to a parent for graph hierarchy tracking. |

```json
{
  "workspace_id": "ws-1",
  "binding": {"kind": "agent", "agent_id": "code-reviewer"},
  "initial_instructions": "Review the diff in diff.patch and write findings to report.md",
  "auto_start": true
}
```

**`workspaces__cancel_workspace_session`** -- Hard-cancel a session. A created or paused session ends immediately; a running one is preempted at the next safe point. Not for a temporary halt (use `pause_workspace_session`). Returns `{status: "ended", ended_reason: "cancelled"}`.

**`workspaces__list_workspace_sessions`** -- List sessions on a workspace, paginated. Returns `SessionInfo` objects and a `total` count.

**`workspaces__get_workspace_session`** -- Get session state: `{info, status}` where `info` is the `SessionInfo` and `status` is the current lifecycle state (`running` / `waiting` / `paused` / `ended`).

**`workspaces__pause_workspace_session`** -- Request that a running session pause at the next safe point. The session status transitions to `PAUSED`. Not to be confused with yielding (which happens inside a turn); pause is an operator-level halt between turns.

**`workspaces__resume_workspace_session`** -- Request that a paused session resume. Returns `type=conflict` when the session is not currently paused.

**`workspaces__steer_workspace_session`** -- Append a steering user instruction to a running session. The agent sees it on its next turn. Use this to nudge a live agent mid-run without stopping it.

### File tools

**`workspaces__list_workspace_files`** -- List files at a path (default: root) with pagination. `recursive=true` walks the whole tree. Each item is a `FileEntry` (path, kind, size_bytes, modified_at). Example:

```json
{"workspace_id": "ws-1", "path": "src", "recursive": true}
```

**`workspaces__get_workspace_file_info`** -- Fetch the `FileEntry` for a single path (file, directory, or symlink). Returns `type=not-found` when missing; `type=bad-request` on path-escape attempts.

**`workspaces__read_workspace_file`** -- Read a file's content. `encoding=text` (default) returns UTF-8 decoded content. `encoding=base64` returns raw bytes as base64 -- use this for binary files. Returns `{path, encoding, content, size_bytes}`.

**`workspaces__write_workspace_file`** -- Create or overwrite a file. `encoding=text` (default) writes UTF-8. `encoding=base64` decodes the content from base64 before writing. Creates parent directories as needed. Refuses to write inside reserved `.state/` or `.tmp/` trees.

**`workspaces__delete_workspace_file`** -- Delete a file or empty directory. Refuses to delete the workspace root or paths inside `.state/` or `.tmp/`.

### Log tool

**`workspaces__get_workspace_log`** -- Fetch up to `limit` recent commits from the workspace's `.state` git repository, newest first. Each commit carries parsed `X-Primer-*` trailers (workspace, session, agent, op, tool, call) for structured rendering. Use this to inspect a workspace's turn history.

### Yielding tools

These two tools require a workspace session context and are excluded from the MCP surface.

**`workspaces__watch_files`** -- Parks the agent turn until one or more workspace-relative paths change on disk. See the Yielding tools page for full semantics, parameters, and resume behaviour.

**`workspaces__invoke_graph`** -- Runs a named graph inside the current session and returns its output text. Parks the calling session on any human-in-the-loop step inside the graph run (ask_user, tool approval). See the Yielding tools page.

```ref:features/yielding-tools
Full semantics of watch_files, invoke_graph, and how a session parks and resumes.
```

## What happens after

Once an agent has the workspace toolset, it can manage the full workspace lifecycle without operator involvement. A meta-agent can create providers and templates, materialise workspaces from them, start child sessions with different agent or graph bindings, steer those sessions mid-run, poll them to completion, read their file output, and clean up the workspace when done. This is the dogfooding pattern: an agent that builds and runs other agents inside isolated environments.

The workspace toolset is the same surface the REST API exposes; every tool corresponds to a REST endpoint. Anything the console does in the Workspaces section can be done by an agent via this toolset.

```ref:features/workspace-providers
Provider types (local, container, Kubernetes) and how to set them up.
```

```ref:features/sessions
Session lifecycle states and the ask_user / approval surfaces.
```

```ref:features/yielding-tools
The two yielding workspace tools in detail.
```
