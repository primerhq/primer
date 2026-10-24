---
title: "Observe a session"
summary: "List sessions, read one session's state and transcript, and tap live events."
order: 3
tools: [workspaces__list_workspace_sessions, workspaces__get_workspace_session, workspaces__read_workspace_session_messages, workspaces__workspace_tap]
related: [sessions/control-a-session, sessions/find-sessions, triggers/waiting-for-events]
---
## What it is
Four read-only tools tell you what a session is doing: `workspaces__list_workspace_sessions` lists one workspace's sessions, `workspaces__get_workspace_session` reads one session's lifecycle, `workspaces__read_workspace_session_messages` reads its transcript, and `workspaces__workspace_tap` drains the live events of every session in a workspace.

## Concepts
- **`list_workspace_sessions`** pages through one workspace (`limit` 1 to 200, default 20, and `offset`). The answer is `{items, offset, length, total}`; each item is a session summary with `session_id`, `agent_id`, `status`, `ended_reason`, `last_turn_error`, `parent_session_id` and the times. A graph session's `agent_id` reads `graph:<graph id>`.
- **`get_workspace_session`** answers `{info, status}`: `status` is the lifecycle (`created`, `running`, `waiting`, `paused`, `ended`) and `info` carries `ended_reason`, `ended_detail`, `last_turn_error` and the times, taken from the durable row. It does not carry `session_state` or the park key. For those, read the full row: `workspaces__find_sessions` ([find sessions](sessions/find-sessions)), or `workspaces__list_my_sessions` for your own workspace (`state`, `parked_on`).
- **`read_workspace_session_messages`** returns `{items, total, offset, limit}`. Each record is `{seq, kind, payload, created_at}`; `seq` rises by one per record. The kinds you meet most: `user_input`, `assistant_token` (the assistant's text, coalesced), `reasoning`, `tool_call`, `tool_result`, `yielded` and `resumed` (a park and its end), `done`, `cancelled`, `error` and `invocation_divider` (a restart).
  - Poll with `after_seq`: pass the last `seq` you saw and get only newer records.
  - `tail: true` returns the newest `limit` records instead of the oldest.
  - An `error` record carries `payload.code` and `payload.message`; branch on the code. One with `fatal: false` is a notice: the turn went on.
  - A `user_input` record sent by another session carries `payload.peer` (the sender's session, workspace, agent or graph, and the hop count).
- **`workspace_tap`** drains events for every session in a workspace since a cursor: `{events, next_cursor}`. Pass `next_cursor` back as `cursor` on the next call; the `cursor` field inside each event is not a resume token. An empty `events` with the same `next_cursor` means nothing new. `wait_seconds` (up to 30) long-polls when the drain would be empty. Each event has `session_id`, `agent_id` or `graph_id`, `class` (the record kind, or `graph_transition`), `ts` and `payload`.
- **`selector`** narrows the tap: `sessions` is a predicate on the session rows, `events` a predicate on each event, both in the storage predicate shape (`{"left": {"kind": "field", "name": ...}, "op": "=", "right": {"kind": "value", "value": ...}}`).
- **Why a session is parked.** The full row's `parked_event_key` starts with the kind of wait:

| prefix | the session waits on |
|---|---|
| `ask_user:` | a person's answer to `system__ask_user` |
| `tool_approval:` | an approval decision |
| `evwait:` | `system__wait_for_event` |
| `timer:` | `workspace_ext__sleep` |
| `watch:` | `workspace_ext__watch_files` |
| `trigger:` | `workspace_ext__subscribe_to_trigger` |

  `list_my_sessions` names the same wait in words in `parked_on` (`ask_user`, `approval`, `wait_for_event`, `sleep`, `watch_files`, `subscribe_to_trigger`).
- From a session outside the default workspace (prime) these tools reach only your own workspace; another one answers `not-found`, as an unknown id does.

## How to
1. List the workspace's sessions, or find them across workspaces with `workspaces__find_sessions`.
2. Read the lifecycle of the one you care about with `get_workspace_session`.
3. Read its transcript; keep the last `seq` and poll with `after_seq`.
4. To follow several sessions at once, drain `workspace_tap` with `next_cursor` and a short `wait_seconds`.
5. To wait without polling, from inside a session, use `system__wait_for_event` on `session.ended`, `session.parked` or `session.turn_failed` ([waiting for events](triggers/waiting-for-events)).

## Tool calls
### List the sessions of a workspace
One page of the workspace's sessions, with the total count.

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "ws-docs", "limit": 20, "offset": 0}}
```

Reference: [`workspaces__list_workspace_sessions`](reference/tools/workspaces/list_workspace_sessions)

### Read one session's lifecycle
The status, the end reason and the last turn's failure, if any.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-docs", "session_id": "sess-3f9a1c8d2b7e"}}
```

Reference: [`workspaces__get_workspace_session`](reference/tools/workspaces/get_workspace_session)

### Poll the transcript
Only the records after the last `seq` you saw.

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_session_messages", "arguments": {"workspace_id": "ws-docs", "session_id": "sess-3f9a1c8d2b7e", "after_seq": 42}}
```

Read the newest records instead:

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_session_messages", "arguments": {"workspace_id": "ws-docs", "session_id": "sess-3f9a1c8d2b7e", "tail": true, "limit": 20}}
```

Reference: [`workspaces__read_workspace_session_messages`](reference/tools/workspaces/read_workspace_session_messages)

### Drain the live events of one session
The tool calls of one session, waiting up to 10 seconds when nothing is new. Pass the returned `next_cursor` as `cursor` next time.

```json
{"toolset_id": "workspaces", "tool_name": "workspace_tap", "arguments": {"workspace_id": "ws-docs", "selector": {"sessions": {"left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "sess-3f9a1c8d2b7e"}}, "events": {"left": {"kind": "field", "name": "class"}, "op": "=", "right": {"kind": "value", "value": "tool_call"}}}, "limit": 50, "wait_seconds": 10}}
```

Reference: [`workspaces__workspace_tap`](reference/tools/workspaces/workspace_tap)

## Gotchas
- Never read `.state/sessions/<id>/messages.jsonl` with `workspaces__read_workspace_file`: raw `.state` reads need an admin. Use `read_workspace_session_messages`.
- A session parked on a gate has `status` `running`. Read `session_state` from the full row before you decide it is busy.
- An answer to a parked `ask_user` and an approval decision are made by a person, in the console or a bound channel. No tool answers them, and a steer or a message is never the answer.
- A missing, empty or unreadable `cursor` starts a fresh drain from the beginning of every session's stream. Keep the `next_cursor` of each workspace you follow.
- `wait_seconds` blocks your turn while it waits. Keep it short, or wait with `system__wait_for_event`, which parks the turn instead.

## Related
[control a session](sessions/control-a-session), [find sessions](sessions/find-sessions), [messaging other sessions](sessions/messaging-other-sessions), [waiting for events](triggers/waiting-for-events).
