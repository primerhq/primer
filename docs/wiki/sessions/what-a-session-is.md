---
title: "What a session is"
summary: "A session is one agent or graph working inside a workspace: its states, where it runs and where its record lives."
order: 1
related: [sessions/start-a-session, sessions/observe-a-session, workspaces/what-a-workspace-is]
---
## What it is
A session is one agent or graph working inside a workspace. It has a durable row in storage, a transcript inside the workspace, and it runs one turn at a time on whichever worker is free. You start one with `workspaces__create_workspace_session` ([start a session](sessions/start-a-session)) and drive it with the session tools, never with a process handle.

## Concepts
- **The row.** Every session read serves the same row: `id` (`sess-<hex>`), `workspace_id`, `binding` (`{"kind": "agent", "agent_id": ...}` or `{"kind": "graph", "graph_id": ...}`), `status`, `session_state`, `turn_status`, `ended_reason`, `last_turn_error`, `parked_event_key`, `parent_session_id` and `initiated_by` (who started it).
- **`status`** is the stored lifecycle: `created` (the row exists, nothing has run), `running` (a turn is armed, running, or parked on a yielding tool), `waiting` (alive and idle between turns), `paused` (a person paused it) and `ended` (terminal).
- **`session_state`** is one value derived on every read. Use it to answer "is it working right now":

| `session_state` | meaning |
|---|---|
| `running` | a turn is in flight now |
| `parked` | it waits on a yielding tool (an answer, an approval, an event, a timer), or it rests after at least one finished turn |
| `waiting` | it is alive and has never finished a turn (just created, or its first turn failed) |
| `ended` | it is over; see `ended_reason` |

  Filter on `session_state` `running` to find sessions that work right now. `status` `running` also covers a parked session and one queued for a worker.
- **`turn_status`** is the scheduler's view: `idle`, `arming`, `claimable` or `running`. One wake arms one turn: the wake that wins writes `arming` with a conditional write, and a message that arrives meanwhile is queued for the next turn boundary. A message queued behind older ones waits its turn, one per turn boundary. A sweep that runs on one elected process delivers a queued message that no turn boundary would pick up.
- **Interactive or autonomous.** An agent session is interactive: after a clean turn it rests (`status` `waiting`, served as `parked`) and waits for the next message. A graph session is autonomous: it ends after a clean run. Set `autonomous: true` at start to make an agent session end after its turn, as a graph does.
- **A failed model call does not end an interactive session.** When the provider fails (a server error, a rate limit, a dropped connection, a timeout) after its retries, the session rests `waiting` with `last_turn_error` (`{code, at}`), and your next message continues it. An autonomous session, a rejected request (bad credentials, a bad request, an overflowing context) and a crash still end it `failed`.
- **`ended_reason`** is one of `completed`, `failed`, `cancelled`, `workspace_lost` (the workspace became unreachable for good), `force_deleted` or `tool_turn_cap` (an autonomous session that hit its agent's `max_tool_turns`).
- **Rank.** A session runs at the rank of the person or token that started it (`initiated_by`), capped by that principal's current role. A session whose creator was deleted or disabled, or whose token was revoked, does not run at all: its turn fails with the code `run_refused`.
- **Reach.** A session in a workspace reaches only its own workspace: a session tool answers another workspace exactly as an unknown id (`not-found`), and `workspaces__find_sessions` finds nothing there. A session in the default workspace (prime) and a caller with no session (a person over MCP) reach every workspace. See [prime](workspaces/prime).
- **No locality.** Any worker, in any process or pod, may run the next turn, and a parked session resumes on whichever worker is free. Keep the `id` and drive the session through the tools.
- **Where the record lives** (paths relative to the workspace):
  - `.state/sessions/<id>/`: the session's history and notes, committed to the workspace's git state repo. Read a transcript with `workspaces__read_workspace_session_messages`; a raw `.state` read through `workspaces__read_workspace_file` needs an admin.
  - `.tmp/<id>/`: large tool outputs of that session, saved as files.
  - `.state/shared/`: the place sessions of one workspace coordinate through files.

## Gotchas
- `ended` is terminal. Only `workspaces__restart_workspace_session`, or a new message to a restartable session, reopens it ([control a session](sessions/control-a-session)).
- A parked session holds no worker and no compute. It can wait for hours and resume on another worker.
- `auto_start: false` leaves the session `created` and inert: nothing runs it until a message reaches it or a person starts it.
- Do not wait for `ended` on an interactive agent session: it rests after each clean turn and never ends by itself. Start it with `autonomous: true` when you need it to end.
- `session_state` `parked` does not always mean someone must act: a session that rests after a clean turn is `parked` too. Look at `parked_event_key` (empty when it only rests) to tell them apart.

## Related
[start a session](sessions/start-a-session), [observe a session](sessions/observe-a-session), [control a session](sessions/control-a-session), [what a workspace is](workspaces/what-a-workspace-is).
