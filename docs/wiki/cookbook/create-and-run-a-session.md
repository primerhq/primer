---
title: "Create and run a session"
summary: "Run an existing agent in a fresh workspace and collect what it wrote."
order: 1
tools: [workspaces__list_workspace_templates, workspaces__create_workspace, workspaces__create_workspace_session, workspaces__get_workspace_session, workspaces__read_workspace_file, workspaces__delete_workspace]
related: [sessions/start-a-session, sessions/observe-a-session]
---
## Goal

Run an existing agent headlessly in a fresh workspace, wait until it ends and read back the file it wrote.

## Prerequisites

- Run it over MCP or from a session in the default workspace (prime). From a session in any other workspace, `workspaces__create_workspace` answers `type=forbidden`, and the workspace tools reach only that session's own workspace.
- An agent to run. Its id is in the live map [this-install/agents](this-install/agents).
- A workspace template. If none is configured, create one first with `workspaces__create_workspace_template` (out of scope here).

## Steps

### 1. Pick a template

List the materialisation recipes you may use with `workspaces__list_workspace_templates`:

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_templates", "arguments": {}}
```

Response:

```json
{"items": [{"id": "py-base", "description": "Python base image", "provider_id": "local-1"}], "offset": 0, "length": 1, "total": 1}
```

Thread the chosen template `id` into the next step.

### 2. Materialise the workspace

Create the live workspace from the template with `workspaces__create_workspace`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"template_id": "py-base"}}
```

Response:

```json
{"id": "ws-9f2c", "template_id": "py-base", "phase": "pending"}
```

`phase` is `pending` at the answer: the row starts `pending` and is usable at once (start sessions in it now). The health probe turns it `running` at its first good probe, about 30 seconds later, and `failed` after three missed probes. Thread the workspace `id` into the next step.

### 3. Start the session

Bind the agent and start its first turn with `workspaces__create_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "binding": {"kind": "agent", "agent_id": "report-writer"}, "initial_instructions": "Write a one-page summary of README.md to report.md.", "autonomous": true, "auto_start": true}}
```

Response:

```json
{"id": "sess-a1b2", "workspace_id": "ws-9f2c", "status": "running"}
```

Set `autonomous: true` so the session ends on its clean turn. Without it, the session rests `waiting` after each turn, and only a new message or a steer starts the next one. Thread the session `id` into the next step.

### 4. Poll until it ends

Read the session state with `workspaces__get_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2"}}
```

Response:

```json
{"info": {"session_id": "sess-a1b2", "status": "ended", "ended_reason": "completed"}, "status": "ended"}
```

Poll while `status` is `running` or `waiting`. `ended` is the stop condition: `info.ended_reason` is `completed` for a clean exit, `failed` for an error and `cancelled` when you asked for it.

### 5. Read the file it wrote

Read the output back from the workspace with `workspaces__read_workspace_file`:

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_file", "arguments": {"workspace_id": "ws-9f2c", "path": "report.md"}}
```

Response:

```json
{"path": "report.md", "encoding": "text", "content": "Summary of the repository...", "size_bytes": 2048}
```

`encoding` is `text` (UTF-8) by default; for a binary file, pass `"encoding": "base64"`.

### 6. Delete the workspace

Tear the workspace down and free its backend resources with `workspaces__delete_workspace`:

```json
{"toolset_id": "workspaces", "tool_name": "delete_workspace", "arguments": {"id": "ws-9f2c"}}
```

Response:

```json
{"deleted": true, "id": "ws-9f2c"}
```

## Verify

`workspaces__get_workspace_session` reads `status: ended` with `ended_reason: completed`, `workspaces__read_workspace_file` returns the file, and `workspaces__delete_workspace` answered `deleted: true`.

## Gotchas

- `waiting` where you expected `ended` means the turn ended without finishing the work (the assistant ended on a question, a `max_tokens` stop or a content filter, or the model call failed on a transport error): the session rests, and a new message or a steer starts the next turn. A session parked on a question, an approval or a timer keeps `status: running`, and the last `yielded` record of the transcript names what it waits on. Follow [cookbook/monitor-and-resume-a-parked-session](cookbook/monitor-and-resume-a-parked-session).
- Stopping is not cancelling. `workspaces__interrupt_workspace_session` stops the turn that is running and keeps the session alive; `workspaces__cancel_workspace_session` ends it (`ended_reason: cancelled`).
- Deleting the workspace ends every open session in it, and it is refused (`type=forbidden`) while one of them runs at a higher rank than you: end the session first.
- `workspaces__create_workspace` answers `type=backend-error` when the live instance does not answer within 30 seconds: the image must run the primer workspace runtime. Nothing is left behind.

## Related

- [Start a session](sessions/start-a-session)
- [Observe a session](sessions/observe-a-session)
