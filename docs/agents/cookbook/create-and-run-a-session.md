---
slug: cookbook/create-and-run-a-session
title: Create And Run A Session
summary: Run an existing agent headlessly against a fresh workspace and collect its result over MCP.
mcp_tools:
  - workspaces::list_workspace_templates
  - workspaces::create_workspace
  - workspaces::create_workspace_session
  - workspaces::get_workspace_session
  - workspaces::read_workspace_file
  - workspaces::cancel_workspace_session
  - workspaces::delete_workspace
---

## Goal
Run an existing agent headlessly inside a new workspace and read back the file it produced.

## Prerequisites
- A registered agent to run; find one with `search::search_agents` if you do not know its id.
- A workspace template; list them with `workspaces::list_workspace_templates`. If none is configured, create one first with `workspaces::create_workspace_template` (out of scope here).

## Steps
### 1. Pick a template
`workspaces::list_workspace_templates`
```json
{}
```
Response:
```json
{ "items": [ { "id": "py-base" } ] }
```
Use a returned `id` as the `template_id`. If exactly one is configured you can note it and skip the listing.

### 2. Create the workspace
`workspaces::create_workspace`
```json
{ "template_id": "py-base" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Thread `id` ("ws-1") into every later call as `workspace_id`. Wait until `phase` is `running` before starting a session.

### 3. Start the session
`workspaces::create_workspace_session`
```json
{
  "workspace_id": "ws-1",
  "binding": { "kind": "agent", "agent_id": "code-reviewer" },
  "initial_instructions": "Summarise README.md into SUMMARY.md",
  "auto_start": true
}
```
Response:
```json
{ "id": "ses-1", "status": "running" }
```
With `auto_start: true` the session begins immediately. Thread `id` ("ses-1") as `session_id`.

### 4. Poll until done
`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```
Re-call on an interval until `status` is `ended`. Status moves `created` then `running` then `ended`.

### 5. Collect the output
`workspaces::read_workspace_file`
```json
{ "workspace_id": "ws-1", "path": "SUMMARY.md" }
```
Response:
```json
{ "path": "SUMMARY.md", "content": "..." }
```
Read whatever file the agent was told to write.

## Verify
`status` is `ended` with `ended_reason: "completed"` (not `failed`), and the expected output file exists and is non-empty.

## Gotchas
- If `status` stays `running` past your timeout the agent may be parked on a yielding tool; the surface shows `status: "waiting"`. See `cookbook/monitor-and-resume-a-parked-session`.
- To stop a run early, call `workspaces::cancel_workspace_session`; it ends with `ended_reason: "cancelled"`.
- Clean up the sandbox when finished with `workspaces::delete_workspace` `{ "id": "ws-1" }`.

## Related
- `sessions`, `agents`, `workspaces`
- `cookbook/run-a-graph-and-collect-results`
- `cookbook/monitor-and-resume-a-parked-session`
