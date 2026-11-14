---
title: Workspace status
summary: "The status of every workspace and session your user may read: which are running, which are parked, and what a park waits on, from live state."
audience: admin
tools: [workspaces__list_workspaces, workspaces__find_sessions, workspaces__list_workspace_sessions, workspaces__get_workspace_session]
related: [purpose-map, runbooks/moderate-a-request, runbooks/conventions, sessions/observe-a-session]
---

## Goal
Answer, from live state, which workspaces and sessions exist for your user, which are running and which are parked - and what a parked session is waiting on. No state in this answer comes from exploration: it is the rows the reads above serve.

## Preconditions
- the operator's turn can reach the workspace reads.
  - check: step 1 IS the check: `workspaces__list_workspaces` -> expected: a page of rows (at least the default workspace, the row with `is_default: true`). An empty page means the person behind the run reads no workspace: report that, and stop.
- nothing else: this flow writes nothing, so no gate pre-read is needed.

## Roles and gates
- runner: `user` (the reads are scoped to the person behind the run: a workspace that person holds no level on answers exactly as one that does not exist).
- gated steps: none - the flow writes nothing, and all four reads ship with no approval gate.

## Steps
### 1. Enumerate the visible workspaces
Call `workspaces__list_workspaces` ([reference](reference/tools/workspaces/list_workspaces)):

```json
{"toolset_id": "workspaces", "tool_name": "list_workspaces", "arguments": {"limit": 20}}
```

Expected: a page of `Workspace` rows. This lists exactly the workspaces the person behind the run may read (an admin sees every one). While the page reports more rows than it served, continue with `offset`.

### 2. Survey the sessions across all of them
Two cross-workspace reads answer the bookkeeper question in one call each: `workspaces__find_sessions` ([reference](reference/tools/workspaces/find_sessions)) with the derived state as the filter:

```json
{"toolset_id": "workspaces", "tool_name": "find_sessions", "arguments": {"state": "parked", "limit": 20}}
```

```json
{"toolset_id": "workspaces", "tool_name": "find_sessions", "arguments": {"state": "running", "limit": 20}}
```

Expected: the full session rows across every readable workspace, newest first. A session blocked on a yielding tool carries `parked_event_key`, whose prefix names what the park waits on: `ask_user:` a person's answer; `tool_approval:` an approval decision; `evwait:` `system__wait_for_event`; `timer:` `workspace_ext__sleep`; `watch:` `workspace_ext__watch_files`; `trigger:` `workspace_ext__subscribe_to_trigger`. The derived `parked` state also includes sessions resting after a completed turn or paused after work; those can be without a park key. Report them as resting or paused, without inventing a wait reason.

`find_sessions` has no offset or cursor: it returns at most `limit` rows (maximum 200). If `total` exceeds the returned count, increase `limit` to 200, then narrow with `workspace_id` or `agent_id` as needed. Report any remaining truncation; do not claim the result is exhaustive. Step 3 can enumerate a workspace's session summaries with `offset`, but those summaries do not carry park keys.

### 3. Per-workspace detail, when the question names a workspace
Call `workspaces__list_workspace_sessions` ([reference](reference/tools/workspaces/list_workspace_sessions)):

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "<the workspace id>", "limit": 20}}
```

Expected: `SessionInfo` items with the raw lifecycle `status` (`created`, `running`, `waiting`, `paused`, `ended`), the `agent_id`, the `binding`, and - for an ended one - the `ended_reason`. A `waiting` status is a parked or rested session: the reason is the `parked_event_key` from step 2, not in this row.

### 4. One session's full state
Call `workspaces__get_workspace_session` ([reference](reference/tools/workspaces/get_workspace_session)):

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "<the workspace id>", "session_id": "<the session id>"}}
```

Expected: `{info, status}` - the `SessionInfo` plus the current lifecycle state. The answer carries no `parked_event_key`: the park's reason comes from the step 2 row for that session (or from `workspaces__list_my_sessions` for your own workspace, which names the wait in words in `parked_on`).

### 5. Answer
State it per workspace: which sessions are running, which are parked, and - when a park key exists - what it waits on, from the `parked_event_key` prefix. A session without a park key has no recorded yielding-tool wait; it may be resting or paused. A workspace with no running or parked session is idle, not missing.

## Verification
- every claim in the answer traces to a row served by steps 1-4 (the ids match what the reads returned).
- when a parked session carries a `parked_event_key`, the stated wait matches its prefix; without a park key, no yielding-tool wait is claimed.
- the end state is the read itself: this flow is done when the answer is built from live rows - there is no write whose durable result to read.

## Refusal and failure
- type=not-found (a workspace id or a session id) -> the person behind the run holds no level on it: the id answers exactly as a missing one. Report it as not visible; do not probe the raw REST API or the database for the mechanism.
- type=forbidden (a read below the level the tool needs) -> the level is insufficient for that read: name the read and stop.
- an empty page for `state: parked` -> nothing is parked: say so; the `running` page separately reports active work.
- a raw `waiting` session absent from the `parked` results may be a fresh session whose derived state is still `waiting`, or the result may be truncated. Check the derived state and narrow the filters before claiming it is missing. A session resting after a completed turn reads `parked` even without a yielding-tool wait.

## Related
- [purpose-map](purpose-map): the index row this runbook serves.
- [runbooks/moderate-a-request](runbooks/moderate-a-request): the moderator flow whose new workspaces this flow surveys.
- [sessions/observe-a-session](sessions/observe-a-session): the session observation tools' concept page (the park prefixes live there).
- [runbooks/conventions](runbooks/conventions): the authoring rule this runbook follows.
