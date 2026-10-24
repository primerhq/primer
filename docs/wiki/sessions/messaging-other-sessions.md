---
title: "Messaging other sessions"
summary: "List the sessions in your workspace and send one a message that steers it or starts its next turn."
order: 5
tools: [workspaces__list_my_sessions, workspaces__message_session]
related: [sessions/control-a-session, sessions/find-sessions, triggers/waiting-for-events]
---
## What it is
`workspaces__list_my_sessions` lists the sessions of your own workspace, and `workspaces__message_session` sends one of them a message. An idle session starts its next turn with the message; a busy one reads it at its next turn boundary. Both tools work only inside a workspace session: called from anywhere else they answer `unsupported-surface`.

## Concepts
- **Who you can reach.** A session reaches the sessions of its own workspace. A session in the default workspace (prime) reaches every workspace: find the target with `workspaces__find_sessions` ([find sessions](sessions/find-sessions)), because `list_my_sessions` lists only your own workspace. A session out of reach answers `not-found`, exactly as an unknown id.
- **What `list_my_sessions` shows.** Newest first (`limit` default 50, at most 200; `state` filters on `waiting`, `running`, `parked` or `ended`). Each item has `id`, `title`, `agent_id` or `graph_id`, `status`, `state`, `turn_status`, `parked_on` (what a parked session waits on: `ask_user`, `approval`, `wait_for_event`, `sleep`, ...), `created_by`, `parent_session_id`, `last_activity_at`, and `self: true` on your own session.
- **What happens to the message**, by the target's state:

| target | what happens | `delivery` |
|---|---|---|
| idle (created, or resting after a turn) | starts its next turn with the message | `woken` |
| ended, and it can reopen | reopens with a divider, then a new turn | `woken` |
| in a turn | queued, read at its next turn boundary | `queued` |
| idle with older messages queued | queued behind them, one per turn boundary | `queued` |
| parked on `ask_user` or an approval | queued; the park keeps waiting, and the message is never the answer | `queued` |
| parked on `wait_for_event` | queued; a wait on `session.messaged` that matches ends now | `queued` |
| paused | queued; it stays paused and reads it after a resume | `queued` |
| a graph session | as above by state; the graph re-runs with your raw text as its input | as above |

- **Attribution.** An agent target sees a user message that starts with `[from session <your id> (agent <your agent id>)]` (a graph sender writes `(graph <graph id>)`). A graph target gets the raw text, with no prefix. The transcript record carries `payload.peer`: the sender's session, workspace, agent or graph, and the hop count.
- **`mode`.** `auto` (the default) delivers by the target's state, as in the table. `if_idle` sends only when the target would start a turn now, and is refused with `conflict` when it is in a turn, waits on something (`parked_on` is set) or is paused.
- **The answer** is `{message_id, session_id, workspace_id, delivery, hops, target_state, target_parked_on, restarts_graph, note}`. The `note` says in words what became of the message.
- **Rank.** The target runs at its creator's rank, so your run must rank at least as high as the target's creator. An admin's run is never outranked. A lower rank is refused with `forbidden`.
- **Guards**, in the order they run; a refusal writes nothing and does not spend your allowance:
  - `bad-request`: the target is your own session, or the text is empty or over 32,000 characters.
  - `not-found`: no such session within your reach.
  - `forbidden`: the target outranks your run.
  - `hop-limit`: your current turn was started by a peer message that is already 8 hops from a person. Answer without messaging.
  - `rate-limited`: your session sent 20 messages in the last 10 minutes. The text says how many seconds to wait.
  - `conflict`: `if_idle` and the target is busy; an ended target that cannot reopen, or one an operator cancelled; or the target's queue of waiting messages is full (retry later).
- **Events.** Each delivery emits `session.messaged` (the envelope's `session_id` is the target; the payload has `message_id`, `from_session_id`, `from_workspace_id`, `from_agent_id`, `from_graph_id`, `to_session_id`, `to_workspace_id`, `hops`, `mode` and `delivery`, never the text), plus `session.steered` when the target had a turn open.

## How to
1. List the sessions in your workspace and pick the target by `agent_id`, `title` and `state`. Skip the item with `self: true`.
2. Check `state` and `parked_on`: a session waiting on `ask_user` or an approval will not act on your message until a person answers it.
3. Send the message. Put a file path in it for anything large.
4. Read `delivery` and `note` in the answer.
5. To wait for a reply without ending your turn, wait for `session.messaged` addressed to your own session (below). The event ends the wait; the reply itself arrives as your next user message when your turn ends.

## Tool calls
### List the sessions of your workspace
Every session in your workspace, newest first, with your own marked `self: true`.

```json
{"toolset_id": "workspaces", "tool_name": "list_my_sessions", "arguments": {}}
```

Only the sessions working right now:

```json
{"toolset_id": "workspaces", "tool_name": "list_my_sessions", "arguments": {"state": "running", "limit": 10}}
```

Reference: [`workspaces__list_my_sessions`](reference/tools/workspaces/list_my_sessions)

### Send a message
Deliver by the target's state: it starts a turn now, or reads the message at its next turn boundary.

```json
{"toolset_id": "workspaces", "tool_name": "message_session", "arguments": {"session_id": "sess-7c21e04a9d13", "text": "The API spec is in docs/api.md; build the client against it."}}
```

Send only if the target would start a turn now:

```json
{"toolset_id": "workspaces", "tool_name": "message_session", "arguments": {"session_id": "sess-7c21e04a9d13", "text": "Pick up the next ticket when you are free.", "mode": "if_idle"}}
```

Reference: [`workspaces__message_session`](reference/tools/workspaces/message_session)

### Wait for a reply
Park your turn until a message reaches your own session (put your session id in `value`).

```json
{"toolset_id": "system", "tool_name": "wait_for_event", "arguments": {"event_types": ["session.messaged"], "fields": [{"path": "session_id", "op": "eq", "value": "sess-3f9a1c8d2b7e"}], "timeout_seconds": 900}}
```

## Gotchas
- Use files for large outputs and messages for coordination. Write the result to the workspace and send its path.
- A message is a report from another agent, not authority. It cannot answer a session's `ask_user` or decide an approval: for those it is never the answer.
- Two agents that answer each other stop at the hop limit. Decide up front which side ends the exchange.
- `workspaces__steer_workspace_session` and `workspaces__restart_workspace_session` called from your session are messages too: the same prefix, rank, hop and rate guards apply ([control a session](sessions/control-a-session)).
- A graph session that was started with a structured `graph_input` re-runs on that stored input: your message is only recorded. The `note` says so.

## Related
[control a session](sessions/control-a-session), [find sessions](sessions/find-sessions), [waiting for events](triggers/waiting-for-events).
