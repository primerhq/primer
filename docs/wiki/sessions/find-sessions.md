---
title: "Find sessions"
summary: "Find sessions in any workspace by workspace, agent, state or who started them."
order: 6
tools: [workspaces__find_sessions]
related: [sessions/observe-a-session, sessions/messaging-other-sessions, workspaces/prime]
---
## What it is
`workspaces__find_sessions` finds sessions across workspaces by workspace, agent, state or the principal that started them, newest first. It is how the operator in prime watches and manages the sessions of other workspaces.

## Concepts
- **Filters.** Every argument is optional, and the ones you give are ANDed:
  - `workspace_id`: one workspace.
  - `agent_id`: sessions bound to this agent (graph sessions never match).
  - `state`: the derived `session_state`: `waiting`, `running`, `parked` or `ended`. Use `running` for the sessions that work right now.
  - `initiated_by`: the id of the principal that started the session, as in a row's `initiated_by.id`.
  - `limit`: how many rows, 1 to 200, default 20. There is no offset: you get the newest `limit` matches.
- **The answer** is a page, `{items, offset, length, total}`, and each item is the full session row: `id`, `workspace_id`, `binding`, `status`, `session_state`, `turn_status`, `parked_event_key`, `ended_reason`, `last_turn_error`, `initiated_by`, `parent_session_id` and the times. That makes it the tool to read why a session is parked ([observe a session](sessions/observe-a-session)).
- **Reach.** From prime, and with no calling session, it searches every workspace. From a session in any other workspace it finds only that workspace's sessions: naming another `workspace_id` finds nothing, exactly as a workspace that does not exist.
- **Which tool to use.**

| you want | use |
|---|---|
| sessions in other workspaces, or across all of them | `workspaces__find_sessions` |
| one workspace's sessions, paged, as summaries | `workspaces__list_workspace_sessions` |
| the sessions of your own workspace, yours marked, to message one | `workspaces__list_my_sessions` |

## How to
1. Decide the narrowest filter: a workspace and a state, or an agent.
2. Call `workspaces__find_sessions` and read `session_state` and `parked_event_key` on each row.
3. Act on the rows you found: read a transcript, steer, stop or cancel ([control a session](sessions/control-a-session)), or message one ([messaging other sessions](sessions/messaging-other-sessions)).

## Tool calls
### Find the running sessions of one workspace
The sessions of `ws-docs` that have a turn in flight now.

```json
{"toolset_id": "workspaces", "tool_name": "find_sessions", "arguments": {"workspace_id": "ws-docs", "state": "running"}}
```

Reference: [`workspaces__find_sessions`](reference/tools/workspaces/find_sessions)

### Find the latest sessions of one agent
The five newest sessions of the `builder` agent, in every workspace you reach.

```json
{"toolset_id": "workspaces", "tool_name": "find_sessions", "arguments": {"agent_id": "builder", "limit": 5}}
```

### Find the sessions that wait on something
Parked sessions in every workspace you reach; read `parked_event_key` on each row.

```json
{"toolset_id": "workspaces", "tool_name": "find_sessions", "arguments": {"state": "parked", "limit": 50}}
```

## Gotchas
- `state` `parked` includes sessions that only rest after a clean turn. Those have no `parked_event_key`; the ones with a key wait on something.
- Filter on `state`, not on `status`: a session parked on a gate has `status` `running`.
- Ended sessions match too unless you filter them out; ask for the state you need.
- Finding a session does not let you drive it: steering, restarting, cancelling and messaging still check your rank against the session's creator ([control a session](sessions/control-a-session)).

## Related
[observe a session](sessions/observe-a-session), [messaging other sessions](sessions/messaging-other-sessions), [prime](workspaces/prime).
