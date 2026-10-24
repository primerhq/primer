---
slug: cookbook/monitor-and-resume-a-parked-session
title: Monitor And Resume A Parked Session
summary: Handle a session parked on a yielding tool; inspect why and resume, steer, or cancel it.
mcp_tools:
  - workspaces::get_workspace_session
  - workspaces::find_sessions
  - workspaces::list_my_sessions
  - workspaces::steer_workspace_session
  - workspaces::resume_workspace_session
  - workspaces::pause_workspace_session
  - workspaces::interrupt_workspace_session
  - workspaces::cancel_workspace_session
---

## Goal
Inspect a session parked inside its turn on a yielding tool and decide whether to nudge, resume, or give up. A park does not move `status`: the session reads `status: "running"` with `parked_status` set, and `session_state: "parked"` on the full row.

## Prerequisites
- A running session id (`workspace_id` + `session_id`); see `cookbook/create-and-run-a-session`.

## Steps
### 1. Poll the session
`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "info": { "session_id": "ses-1", "status": "running" }, "status": "running" }
```
A session parked inside its turn reads `status: "running"`: a park writes the park columns and leaves `status` alone. The answer carries no park field, so it says the session is alive but not what it waits on.

### 2. Read what it waits on
Read the park fields from the full row: `workspaces::find_sessions` (its `workspace_id`, or `state: "parked"`) answers `session_state` and `parked_event_key`; `workspaces::list_my_sessions` answers `state` and `parked_on` for your own workspace. An `ask_user:` or `tool_approval:` key wants a person's answer; a `timer:`, `watch:` or `trigger:` key waits for an external event and may clear on its own.

### 3. Nudge it with a steering instruction
`workspaces::steer_workspace_session`
```json
{
  "workspace_id": "ws-1",
  "session_id": "ses-1",
  "instruction": "Review the main branch; skip generated files."
}
```
Response:
```json
{ "id": "ses-1", "status": "running", "turn_status": "claimable", "delivery": "woken" }
```
An idle session starts its next turn with the instruction; a parked or busy one queues it (`delivery: queued`) for after the park and the turn. A steer never answers an `ask_user`: answer it with `POST /v1/sessions/{id}/ask_user/respond`.

### 4. Resume a paused session
`workspaces::resume_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "running" }
```
Use this to clear a `paused` status (which you set earlier with `workspaces::pause_workspace_session`). Resume is the inverse of pause.

### 5. Give up
`workspaces::cancel_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "cancelled" }
```
Cancel is terminal.

## Verify
After resuming, re-poll `workspaces::get_workspace_session`; the session has transitioned out of `paused` to `running` (or onward to `ended`). After steering, read `delivery`: `woken` means the session is `running` with `turn_status: "claimable"`; `queued` means the instruction waits and the session keeps its status until its turn, its park or its pause ends. After cancel, `status` is `ended` with `ended_reason: "cancelled"`.

## Gotchas
- A parked session is not `waiting` and not `paused`. A park keeps `status: "running"` with `parked_status` set (see `yielding`); `waiting` means the turn ended and the session rests between turns; `paused` is an external halt you applied.
- Pause vs cancel: pause is resumable, cancel is terminal (`ended_reason: "cancelled"`). Do not cancel a session you intend to continue.
- A parked session has no turn to stop, so `workspaces::interrupt_workspace_session` is refused with `type=conflict` and records nothing. Resume or steer it, or cancel it.
- Steering a session that has `ended` reopens it and starts a new turn with the instruction. It answers `type=conflict` only when the session cannot reopen (`ended_reason` `force_deleted`, or `workspace_lost` while the workspace is down).
- A paused session stays paused: the instruction is queued (`delivery: queued`) and the session reads it after you resume it.

## Related
- `yielding`, `sessions`
- `cookbook/create-and-run-a-session`
- `cookbook/discover-a-capability`
