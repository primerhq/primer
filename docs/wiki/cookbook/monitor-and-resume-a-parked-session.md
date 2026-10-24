---
title: "Monitor and resume a parked session"
summary: "Find out why a session is parked, then steer, resume, stop or cancel it."
order: 2
tools: [workspaces__get_workspace_session, workspaces__read_workspace_session_messages, workspaces__steer_workspace_session, workspaces__resume_workspace_session, workspaces__interrupt_workspace_session, workspaces__cancel_workspace_session]
related: [sessions/control-a-session, sessions/observe-a-session]
---
## Goal

Find out why a session that stopped moving is waiting, then choose the move: steer it, resume it, stop its turn or cancel it.

## Prerequisites

- A `workspace_id` and a `session_id` to watch, for example from [cookbook/create-and-run-a-session](cookbook/create-and-run-a-session).
- Run it over MCP or from a session in the default workspace (prime). From a session in any other workspace, the workspace tools reach only that session's own workspace: another one answers `type=not-found`, as an unknown id does.
- The rank rule: `workspaces__steer_workspace_session`, `workspaces__resume_workspace_session`, `workspaces__interrupt_workspace_session` and `workspaces__cancel_workspace_session` refuse a session that runs at a higher rank than you (or your run) with `type=forbidden`. An admin is never outranked, a person over MCP may always act on a session they started, and a session may always act on itself.

## Steps

### 1. Poll the session

Read the session state with `workspaces__get_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2"}}
```

Response:

```json
{"info": {"session_id": "sess-a1b2", "status": "running"}, "status": "running"}
```

`status` is the lifecycle state: `running` (a turn is in flight, or parked on a yielding tool), `waiting` (a turn ended, resting between turns), `paused` or `ended`. A park does not move `status`: a session parked on a question, an approval or a timer reads `running`, and the answer has no park field to say so (the full row, with the park fields, is in `workspaces__find_sessions`, or `workspaces__list_my_sessions` for your own workspace). A session that stopped moving reads `running` (parked), `waiting` (resting) or `paused`; the transcript says which, so read it next.

### 2. Read the transcript tail

Read the last records with `workspaces__read_workspace_session_messages` and `tail: true`:

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_session_messages", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2", "tail": true, "limit": 20}}
```

Response:

```json
{"items": [{"seq": 9, "kind": "yielded", "payload": {"event_key": "ask_user:sess-a1b2:call-3f", "tool_name": "ask_user", "tool_call_id": "call-3f"}, "created_at": "2026-10-24T13:02:11Z"}], "total": 10, "offset": 0, "limit": 20}
```

The last record says what the session waits on. A `yielded` record means it parked inside its turn on a yielding tool; the payload carries the `event_key` and the tool's name. A `done` record means the turn completed and the session rests between turns.

| What you found | It means | The move |
|---|---|---|
| `status: running`, the last `yielded` `event_key` starts with `ask_user:` or `tool_approval:` | a person decides (an answer, an approval) | only a person decides it, in the console; a queued steer is never an answer. Tell the user which session is parked and what it asks |
| `status: running`, the `event_key` starts with `timer:`, `watch:` or `trigger:` | it waits on something that needs no decision | nothing to do: it resumes when the event fires; cancel it if it should not |
| `status: waiting`, the last record is `done` | the turn completed and the session rests | steer it to start the next turn |
| `status: paused` (with `pause_requested: true` on the row when a pause landed mid-turn) | a pause was requested | resume it |
| `status: running` longer than it should | the turn is slow or stuck | stop the turn, then steer |
| it will not come back | the work is lost | cancel it |

### 3. Steer the session

Send the redirect with `workspaces__steer_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "steer_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2", "instruction": "Skip the flaky test and summarise the rest."}}
```

Response:

```json
{"id": "sess-a1b2", "status": "running", "delivery": "queued"}
```

An idle or ended session starts its next turn with the instruction (`delivery: woken`). A busy, parked or paused one reads it at its next turn boundary, so the answer is `delivery: queued` until it can. A steer is never the answer to the session's `ask_user`, and it does not lift a pause: a paused session holds a steer no person wrote until it is resumed or a person messages it. Over MCP, the caller must rank at least as high as the session's creator, or the steer is refused (`type=forbidden`); a session an operator cancelled is refused (`type=conflict`).

### 4. Resume a paused session

Resume it with `workspaces__resume_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "resume_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2"}}
```

Response:

```json
{"id": "sess-a1b2", "status": "running", "pause_requested": false}
```

A `paused`, `waiting` or never started session goes `running` and is queued for its next turn; the pause flag is cleared. A session already running is answered as it is, with nothing changed. An ended session is refused (`type=conflict`), and so is one whose status changed under the call: read the session and try again.

### 5. Stop a running turn

Stop the turn without ending the session with `workspaces__interrupt_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "interrupt_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2"}}
```

Response:

```json
{"id": "sess-a1b2", "interrupt_requested": true}
```

`interrupt_requested: true` means the Stop was recorded; the session lands in `waiting` with its history intact (`paused` when a pause is pending, `pause_requested: true`), and the next message or steer continues it. `interrupt_requested: false` means nothing was running (the session was idle, paused or not started). It is refused (`type=conflict`) when the session has ended or is parked waiting on an approval, an answer or a timer: cancel it instead.

### 6. Cancel the session

End it with `workspaces__cancel_workspace_session`:

```json
{"toolset_id": "workspaces", "tool_name": "cancel_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2"}}
```

Response:

```json
{"id": "sess-a1b2", "status": "ended", "ended_reason": "cancelled"}
```

A created or paused session ends immediately; a running one is preempted at the next safe point.

## Verify

Every move is confirmed by the next poll. After a steer or a resume, `status` reads `running` again and the transcript shows the new instruction. After a stop, `status` reads `waiting` with the history intact. After a cancel, `status` reads `ended` with `ended_reason: cancelled`.

## Gotchas

- A pause does not cut the running turn short: the turn in flight finishes first (or a Stop cuts it short), and the session then reads `paused` with `pause_requested: true` still set. To stop it now and keep it stopped, interrupt it, then pause it.
- A steer to a busy or parked session is queued, not delivered now, and it is never the answer to its `ask_user`: the answer goes through the console.
- A steer from inside a workspace session is a message from that session: the target sees a user message that starts with `[from session <your session id> (agent <your agent id>)]`, and the limits of `workspaces__message_session` apply (hop limit, rate limit).
- Deleting the workspace ends every open session in it (`ended_reason: workspace_lost`), so cancel the session before you delete it.

## Related

- [Control a session](sessions/control-a-session)
- [Observe a session](sessions/observe-a-session)
