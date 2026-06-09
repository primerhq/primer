---
slug: cookbook/run-a-graph-and-collect-results
title: Run A Graph And Collect Results
summary: Execute a multi-step graph over MCP and read its output.
mcp_tools:
  - search::search_graphs
  - workspaces::create_workspace
  - workspaces::create_workspace_session
  - workspaces::get_workspace_session
  - workspaces::read_workspace_file
---

## Goal
Run a multi-step graph headlessly inside a workspace and collect its result over MCP.

## Prerequisites
- A registered graph; find one with `search::search_graphs`.
- A workspace template; list with `workspaces::list_workspace_templates`.

## Steps
### 1. Find a graph
`search::search_graphs`
```json
{ "query": "triage an incident ticket", "top_k": 3 }
```
Response:
```json
{ "items": [ { "id": "incident-pipeline", "score": 0.77 } ] }
```
Thread the chosen `id` into the binding in step 3.

### 2. Create the workspace
`workspaces::create_workspace`
```json
{ "template_id": "py-base" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Wait for `phase: "running"`, then use `id` as `workspace_id`.

### 3. Start a graph-bound session
`workspaces::create_workspace_session`
```json
{
  "workspace_id": "ws-1",
  "binding": { "kind": "graph", "graph_id": "incident-pipeline" },
  "graph_input": { "ticket": "INC-1" },
  "auto_start": true
}
```
Response:
```json
{ "id": "ses-1", "status": "running" }
```
`graph_input` is the payload fed to the graph's Begin node. Thread `id` as `session_id`.

### 4. Poll until done
`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```
A graph runs to completion in one invocation; expect `created` then `running` then `ended` with no mid-graph `waiting`.

### 5. Collect the output
`workspaces::read_workspace_file`
```json
{ "workspace_id": "ws-1", "path": "report.json" }
```
Response:
```json
{ "path": "report.json", "content": "{...}" }
```
Read the file the graph wrote. The full graph state is persisted under `.state/graphs/<session_id>` if you need the per-node trace.

## Verify
`status` is `ended` with `ended_reason: "completed"`, and the graph state directory `.state/graphs/<session_id>` exists alongside your output file.

## Gotchas
- A graph runs straight through with no mid-graph pause (unlike an agent that can park on a yielding tool); see `graphs`.
- `graph_input` is validated against the graph's Begin `input_schema` at create time; a shape mismatch errors immediately at step 3, before the session starts.
- If you bound an agent by mistake, use `binding.kind: "graph"` (not `"agent"`) and `graph_id` (not `agent_id`).

## Related
- `graphs`, `sessions`
- `cookbook/create-and-run-a-session`
- `cookbook/discover-a-capability`
