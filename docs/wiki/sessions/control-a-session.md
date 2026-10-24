---
title: "Control a session"
summary: "Pause, resume, steer, stop, restart or cancel a session, and what each one does to a running turn."
order: 4
tools: [workspaces__pause_workspace_session, workspaces__resume_workspace_session, workspaces__steer_workspace_session, workspaces__interrupt_workspace_session, workspaces__cancel_workspace_session, workspaces__restart_workspace_session]
related: [sessions/messaging-other-sessions, human-in-the-loop/ask-user, approvals/policies]
---
## What it is
Six tools change what a session does. Pick the one verb that matches your intent: steer it, stop its turn, cancel it, restart it, or pause and resume it.

## Concepts
- **Steer** (`workspaces__steer_workspace_session`) gives the session a new instruction. An idle or ended session starts its next turn with it (`delivery: "woken"`); a busy, parked or paused one reads it at its next turn boundary (`delivery: "queued"`). The answer is the session row plus `delivery`. A steer never answers a question the session asked a person, and it does not lift a pause.
- **Stop** (`workspaces__interrupt_workspace_session`) ends the running turn and keeps the session: it lands `waiting` with its history, and the next message continues it. The answer is the session row; read `interrupt_requested`: `true` means a Stop was recorded, `false` means nothing was running and nothing was recorded.
- **Cancel** (`workspaces__cancel_workspace_session`) ends the session for good: `status` `ended`, `ended_reason` `cancelled`. A created, waiting or paused session ends at once; a running or parked one is preempted at the next safe point. Cancel is the way out of a parked session.
- **Restart** (`workspaces__restart_workspace_session`) reopens an ended session in place, with an "invocation N" divider in the same transcript, and starts a turn with `input`. It refuses a session that has not ended (`conflict`). A session that rests after a failed model call (`waiting` with `last_turn_error`) is not ended: steer it instead.
- **Pause and resume** (`workspaces__pause_workspace_session`, `workspaces__resume_workspace_session`) set and clear a pause request on the session's handle in the process that serves the call. They do not write the session row: its `status` does not change, a worker in another process does not see the request, and resume does not start a `created` session. A person pauses, resumes and starts a session from the console.
- **From inside a session**, a steer or a restart is a message from your session: the target sees a user message that starts with `[from session <your id> (agent <your agent id>)]`, and the limits of `workspaces__message_session` apply (rank, `hop-limit`, `rate-limited`; see [messaging other sessions](sessions/messaging-other-sessions)). A restart from a session needs `input`.
- **Rank.** Steer, restart, cancel and resume refuse a session that runs at a higher rank than you (`forbidden`): its creator outranks you, or your run. An admin is never outranked, and a person over MCP may always act on a session they started. Pause and Stop have no rank rule.
- **Reach.** From a session outside the default workspace (prime) only your own workspace is reachable; another one answers `not-found`, as an unknown id does. Prime and a caller with no session reach every workspace.
- **A cancelled session stays cancelled for tools.** A steer, a restart or a message to a session an operator cancelled is refused (`conflict`); only a person reopens it.

## How to
1. Read the session first ([observe a session](sessions/observe-a-session)) and pick the verb from its state.
2. To redirect running work, steer it; to stop a turn that went wrong, interrupt it and then steer it.
3. To end it, cancel it. To run an ended session again, restart it with new `input`.
4. Check the answer: `delivery` for a steer, `interrupt_requested` for a Stop, `status` and `ended_reason` for a cancel.

## Tool calls
### Steer
Add an instruction; the session reads it now or at its next turn boundary.

```json
{"toolset_id": "workspaces", "tool_name": "steer_workspace_session", "arguments": {"workspace_id": "ws-docs", "session_id": "sess-3f9a1c8d2b7e", "instruction": "Focus on the failing test first."}}
```

Reference: [`workspaces__steer_workspace_session`](reference/tools/workspaces/steer_workspace_session)

### Stop the running turn
End the turn and keep the session; read `interrupt_requested` in the answer.

```json
{"toolset_id": "workspaces", "tool_name": "interrupt_workspace_session", "arguments": {"workspace_id": "ws-docs", "session_id": "sess-3f9a1c8d2b7e"}}
```

Reference: [`workspaces__interrupt_workspace_session`](reference/tools/workspaces/interrupt_workspace_session)

### Cancel
End the session with `ended_reason` `cancelled`.

```json
{"toolset_id": "workspaces", "tool_name": "cancel_workspace_session", "arguments": {"workspace_id": "ws-docs", "session_id": "sess-3f9a1c8d2b7e"}}
```

Reference: [`workspaces__cancel_workspace_session`](reference/tools/workspaces/cancel_workspace_session)

### Restart an ended session
Reopen it and start a turn with new input.

```json
{"toolset_id": "workspaces", "tool_name": "restart_workspace_session", "arguments": {"workspace_id": "ws-docs", "session_id": "sess-3f9a1c8d2b7e", "input": "Run the review again against the new branch."}}
```

Reference: [`workspaces__restart_workspace_session`](reference/tools/workspaces/restart_workspace_session)

### Pause and resume
Set, then clear, the pause request on the session's handle.

```json
{"toolset_id": "workspaces", "tool_name": "pause_workspace_session", "arguments": {"workspace_id": "ws-docs", "session_id": "sess-3f9a1c8d2b7e"}}
```

Reference: [`workspaces__pause_workspace_session`](reference/tools/workspaces/pause_workspace_session)

```json
{"toolset_id": "workspaces", "tool_name": "resume_workspace_session", "arguments": {"workspace_id": "ws-docs", "session_id": "sess-3f9a1c8d2b7e"}}
```

Reference: [`workspaces__resume_workspace_session`](reference/tools/workspaces/resume_workspace_session)

## Gotchas
- A success answer to interrupt is not a Stop. Read `interrupt_requested`.
- Interrupt refuses (`conflict`) an ended session and a parked one (waiting on an approval, an answer or a timer) or one resuming from a park: there is no turn to stop. Cancel it instead.
- A Stop lands at the turn's next wait for the model. A tool call already running is cancelled and answered `interrupted: stopped by user (the call may have run, and its result was not recorded)`; a call whose tool cannot be cut short (file writes, session starts) is waited for up to 5 seconds first. Calls that had not started are answered `not run: stopped by user`.
- A graph session stops between graph events, not mid-node. A subagent running inside the turn gets the same Stop.
- A session may stop itself. An autonomous session that does rests `waiting` until something wakes it; a parent waiting for it to end waits on.
- The model does not see the partial text of the round you stopped; the completed rounds stay in its history.
- Steering a session parked on `system__ask_user` or an approval queues the instruction; the park still waits for a person ([ask the user](human-in-the-loop/ask-user), [approval policies](approvals/policies)).
- An ended session that cannot reopen (`force_deleted`, or a workspace that is gone) refuses a steer and a restart with `conflict`.

## Related
[messaging other sessions](sessions/messaging-other-sessions), [observe a session](sessions/observe-a-session), [ask the user](human-in-the-loop/ask-user), [approval policies](approvals/policies).
