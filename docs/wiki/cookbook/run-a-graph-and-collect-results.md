---
title: "Run a graph and collect results"
summary: "Pick a graph, run it in a session and read its output."
order: 3
tools: [system__list_graphs, system__get_graph, workspaces__create_workspace, workspaces__create_workspace_session, workspaces__get_workspace_session, workspaces__read_workspace_session_messages]
related: [graphs/run-a-graph, sessions/start-a-session]
---
## Goal

Run a stored graph headlessly in a fresh workspace, wait until the run ends and read the end node's output from the transcript.

## Prerequisites

- Run it over MCP or from a session in the default workspace (prime). From a session in any other workspace, `workspaces__create_workspace` answers `type=forbidden`, and the workspace tools reach only that session's own workspace.
- A workspace template. If none is configured, create one first with `workspaces__create_workspace_template` (out of scope here).
- A stored graph to run. Step 2 checks its input contract before you build `graph_input`.

## Steps

### 1. List the graphs

Browse the stored graphs with `system__list_graphs`:

```json
{"toolset_id": "system", "tool_name": "list_graphs", "arguments": {}}
```

Response:

```json
{"items": [{"id": "summarise-ticket", "description": "Turn a support ticket into a short brief."}], "length": 1, "total": 1, "next_cursor": null}
```

Thread the chosen graph's `id` into the next step.

### 2. Check the input contract

Fetch the graph with `system__get_graph` and read its `begin` node:

```json
{"toolset_id": "system", "tool_name": "get_graph", "arguments": {"id": "summarise-ticket"}}
```

Response:

```json
{"id": "summarise-ticket", "description": "Turn a support ticket into a short brief.", "nodes": [{"id": "begin", "kind": "begin", "input_schema": {"type": "object", "properties": {"ticket": {"type": "string"}}}}], "max_iterations": null}
```

If the `begin` node sets an `input_schema`, step 4 validates `graph_input` against it and refuses a mismatch or a missing input with `type=validation-error`, and nothing starts. The templates read the input as `initial_input`. Thread the graph's `id` into the next step.

### 3. Materialise the workspace

Create the live workspace from a template with `workspaces__create_workspace`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"template_id": "py-base"}}
```

Response:

```json
{"id": "ws-9f2c", "template_id": "py-base", "phase": "running"}
```

Thread the workspace's `id` into the next step.

### 4. Start the graph run

Bind the graph and start its run with `workspaces__create_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "binding": {"kind": "graph", "graph_id": "summarise-ticket"}, "graph_input": {"ticket": "INC-42"}}}
```

Response:

```json
{"id": "sess-3f9a1c", "workspace_id": "ws-9f2c", "status": "running"}
```

A graph session is autonomous: it ends when the run ends and does not rest between turns. `graph_input` is the run's input, exactly as you pass it. A `profile_id` on the binding is the run's model profile: every `agent` node that names no `profile_id` of its own runs on it. Thread the session's `id` into the next step.

### 5. Poll until the run ends

Read the session state with `workspaces__get_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-3f9a1c"}}
```

Response:

```json
{"info": {"session_id": "sess-3f9a1c", "status": "ended", "ended_reason": "completed", "ended_detail": null}, "status": "ended"}
```

Poll while `status` is `running`. A clean run ends `completed`; a run that fails ends `failed`. While it is `waiting`, a person has a question or an approval to settle, and only a person answers it: in the console, or over the channel the workspace is bound to.

### 6. Read the end node's output

Read the transcript tail with `workspaces__read_workspace_session_messages`:

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_session_messages", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-3f9a1c", "tail": true, "limit": 20}}
```

Response:

```json
{"items": [
  {"seq": 41, "kind": "assistant_token", "node_id": "end", "payload": {"text": "Brief: logins time out since the 24 Oct deploy.", "parsed": {"summary": "logins time out since the 24 Oct deploy", "severity": "high"}, "end_node_id": "end"}, "created_at": "2026-10-24T13:40:02Z"},
  {"seq": 42, "kind": "done", "payload": {"stop_reason": "stop", "raw_reason": "graph_ended", "graph_end": true}, "created_at": "2026-10-24T13:40:02Z"}
], "total": 42, "offset": 0, "limit": 20}
```

The last `assistant_token` record is the end node's rendered output: `end_node_id` names the node and `parsed` holds the template's JSON when it parses. The final `done` record with `graph_end: true` ends the run: `stop_reason` is `stop` when the run completed and `error` when it failed, and a failed run carries an `error` record before it with `code`, `message` and `node_id`.

## Verify

`workspaces__get_workspace_session` reads `status: ended` with `ended_reason: completed`, and the transcript's last `assistant_token` is the end node's rendered output.

## Gotchas

- `initial_instructions` is not the graph's input. It is read, as JSON, only when the `begin` node sets an `input_schema` and no `graph_input` was sent.
- A message to a graph session restarts the graph once the current run is over, or reopens it when it ended. The run takes its input from the stored `graph_input` only: the message is recorded in the transcript, not read as input.
- A run that hits `max_iterations` ends `failed`. Its transcript carries an `error` record with `code: max_iterations_exceeded` and no `node_id`, and the final `done` record's payload carries `ended_reason: max_iterations_exceeded`. Point the cap at a node once with `on_max_iterations` instead of failing.
- A Stop on a graph session takes effect only between graph events, not in the middle of a node. To end the run, cancel the session.
- A graph session reads the stored graph when its run starts. Editing the graph mid-run reaches the next run, not the current step.
- The run's state lives in the workspace, under the session. When you are done, delete the workspace with `workspaces__delete_workspace`.

## Related

- [Run a graph](graphs/run-a-graph)
- [Start a session](sessions/start-a-session)
