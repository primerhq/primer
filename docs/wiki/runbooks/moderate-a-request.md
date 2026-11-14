---
title: Moderate a request
summary: "A user's request that should live apart: plan it with the planner, create a separate workspace, and launch the agents and sessions the plan names in it."
audience: admin
tools: [system__invoke_agent, workspaces__list_workspace_templates, workspaces__create_workspace, workspaces__create_workspace_session, workspaces__get_workspace_session, system__create_agent, system__describe_tool]
related: [purpose-map, runbooks/conventions, runbooks/workspace-status, workspaces/create-and-delete, sessions/start-a-session]
---

## Goal
Run a user's request - one that should live apart from the operator's own session - in a separate workspace, with the agents or sessions the plan names in it.

## Preconditions
- the request is stated: what the work is, in one paragraph.
  - check: none (the user states it; if it is ambiguous, `system__ask_user` before step 1).
- a workspace template to materialise from.
  - check: `workspaces__list_workspace_templates` ([reference](reference/tools/workspaces/list_workspace_templates)) -> expected: at least one row. If none is configured, create one first with `workspaces__create_workspace_template` ([workspaces/templates](workspaces/templates)) - that runbook's surface - and come back.
- the live gate state of the writes this flow makes.
  - check: `system__describe_tool` ([reference](reference/tools/system/describe_tool)) on `workspaces__create_workspace`:

    ```json
    {"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "workspaces", "tool_name": "create_workspace"}}
    ```

    expected on a fresh install: no approval gate (the table below is what a fresh install ships; the operator can retune it, and the answer you just read is the truth for this install). If a gate IS live, step 3 parks on it and a person decides.

## Roles and gates
- runner: `user` (a normal user's request: the operator acts as the session's initiator).
- gated steps (the live gate at run time is read with `system__describe_tool`; the table is what a fresh install ships):
  - step 3 (create the workspace): `workspaces__create_workspace` - shipped gate: none; approver: n/a unless the operator has installed one via [runbooks/approval-policy](runbooks/approval-policy); card shows: the template id and the overrides.
  - step 4 (launch a session): `workspaces__create_workspace_session` - shipped gate: none (as above).
  - step 5, only if the plan names an agent that does not exist yet (create it before step 4): `system__create_agent` - shipped gate: required; approver: per the approval policy (a fresh install sets no admin-only restriction on it); card shows: the agent row (model profile, prompt, tools).

## Steps
### 1. Plan the request
Call `system__invoke_agent` ([reference](reference/tools/system/invoke_agent)) with `agent_id` `planner`, passing the task and a one-paragraph context digest:

```json
{"toolset_id": "system", "tool_name": "invoke_agent", "arguments": {"agent_id": "planner", "prompt": "<the request> Context: <one paragraph>. Plan the work: which workspace template fits, which agent runs what, and the starting instructions for each session."}}
```

Expected: a numbered plan naming the template, the agents, and the per-session instructions. The planner never executes; it returns the plan.

### 2. Pick the template
Call `workspaces__list_workspace_templates` and take the row whose config the plan names:

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_templates", "arguments": {"limit": 20}}
```

Expected: the template row; its `id` is the `template_id` of step 3.

### 3. Create the separate workspace
Call `workspaces__create_workspace` ([reference](reference/tools/workspaces/create_workspace)):

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"template_id": "<the template id from step 2>"}}
```

Expected: the new `Workspace` row (`id`, `phase`). The caller is stamped owner of the workspace - full control of it - which is what lets the operator launch sessions in it next. If the precondition read found a live gate, the turn parks here until a person approves; do not retry the gated call.

### 4. Launch the sessions the plan names
One `workspaces__create_workspace_session` ([reference](reference/tools/workspaces/create_workspace_session)) per session in the plan, in the new workspace:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "<the workspace id from step 3>", "binding": {"kind": "agent", "agent_id": "<the agent the plan names for this session>"}, "initial_instructions": "<the plan's starting instructions for it>", "autonomous": true, "auto_start": true}}
```

Expected: the session row. A session that should run a graph instead takes the graph binding: `{"kind": "graph", "graph_id": "<graph id>"}` with `graph_input` in place of the agent binding's role.

### 5. Create a missing agent first, only if the plan names one (gated)
If the plan names an agent that does not exist yet, create it with `system__create_agent` ([reference](reference/tools/system/create_agent)) BEFORE step 4: the call ships with a required approval gate (the live state, `system__describe_tool`), so the turn parks here until a person approves; do not retry the gated call.

## Verification
- `workspaces__get_workspace_session` ([reference](reference/tools/workspaces/get_workspace_session)) on each launched session:

  ```json
  {"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "<the workspace id>", "session_id": "<the session id>"}}
  ```

  expected: `{info, status}` with the lifecycle `running` (or `waiting` right after a clean turn) and `info.binding.agent_id` equal to the agent the plan named for that session.
- `system__describe_tool` on `workspaces__create_workspace` again: report the observed live gate. The Roles and gates table records shipped defaults; an operator's policy can differ, so follow the live answer rather than assuming a park or an ungated call.
- the bookkeeper flow reaches the new workspace: `workspaces__list_workspaces` lists it, and `workspaces__find_sessions` on it returns the launched sessions ([runbooks/workspace-status](runbooks/workspace-status)).

## Refusal and failure
- type=not-found (the template id) -> the template does not exist or is not readable by the person behind the run: re-run step 2 and pick a listed row.
- type=forbidden (step 3) -> the person may not materialise workspaces with this template (or the overrides name a secret file source, which is admin-only): stop and report which side the gate is on.
- type=approval_required -> parked; a person decides; do not retry the gated call.
- type=conflict (step 3) -> another create reached the workspace id first: retry with a different id, or read the existing row if it is the same request.
- type=validation-error (the session binding) -> the plan's agent id does not resolve or is not readable: re-read the agent roster ([this-install/agents](this-install/agents)) and correct the binding before re-launching.
- a session never leaves `created` / answers a `last_turn_error` in its state -> the workspace stands as built (a partial flow is a reported end state, not a silent retry); read the session's state and report the named error.

## Related
- [purpose-map](purpose-map): the index row this runbook serves.
- [runbooks/workspace-status](runbooks/workspace-status): the bookkeeper flow that surveys the workspace this one built.
- [workspaces/create-and-delete](workspaces/create-and-delete): the workspace's concept page.
- [sessions/start-a-session](sessions/start-a-session): the session's concept page.
- [runbooks/conventions](runbooks/conventions): the authoring rule this runbook follows.
