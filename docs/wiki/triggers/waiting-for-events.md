---
title: "Waiting for events"
summary: "Pause a turn until an event, a trigger or a channel message arrives: which tools pause, and how they resume."
order: 4
tools: [system__wait_for_event, workspace_ext__subscribe_to_trigger, workspace_ext__subscribe_to_channel_event]
related: [human-in-the-loop/ask-user, utilities/sleep-and-watch-files, sessions/messaging-other-sessions]
---
## What it is
A pausing tool parks your turn until something happens. Use one instead of polling: `system__wait_for_event` waits for a platform event, `workspace_ext__subscribe_to_trigger` for a trigger's next fire, and `workspace_ext__subscribe_to_channel_event` for a matching channel event. This page also lists every other tool that pauses.

## Concepts
- **A pause parks the turn.** The call does not answer yet. The session parks, the worker is released, and no compute is used. When the event arrives, any worker resumes the session and the call answers: the event, a timeout or a cancel. The session reads its agent again at the resume, so a prompt or tool change made while it waited applies.
- **Every tool that pauses:**

| Tool | Pauses until | Use it for |
|---|---|---|
| `system__ask_user` | a person answers | a question only a person can answer ([ask the user](human-in-the-loop/ask-user)) |
| `system__wait_for_event` | a platform event matches your filter | a session ending, a trigger firing, a document landing |
| `workspace_ext__sleep` | the seconds pass | a fixed delay ([sleep and watch files](utilities/sleep-and-watch-files)) |
| `workspace_ext__watch_files` | a workspace path changes | a build output or a report file |
| `workspace_ext__invoke_graph` | the graph you run in your session ends | a multi-step workflow ([run a graph](graphs/run-a-graph)) |
| `workspace_ext__subscribe_to_trigger` | the trigger fires next | the next tick of a schedule, the next webhook call |
| `workspace_ext__subscribe_to_channel_event` | a matching event fires a channel trigger | a command such as `/approve` in a room |
| `canvas__canvas_wait` | the user acts on your session's canvas | see [the canvas](human-in-the-loop/canvas) |

- **Through the door.** A pausing tool called through `system__call_tool` pauses the same way, and its answer is the `call_tool` result.
- **Sessions only.** Only a workspace session can park. With no session, `system__wait_for_event` and the subscribe tools answer `unsupported-surface`. MCP clients do not see pausing tools at all: they poll.
- **Time limits.** `system__wait_for_event` takes `timeout_seconds`. Without it, and for the subscribe tools, which take none, the wait ends at the global yield cap: 60 minutes by default.
- **While you wait.** A message from a person or another session queues behind the park ([messaging other sessions](sessions/messaging-other-sessions)). A person can cancel the wait, or the session, from the console when they rank at or above the session.

### `system__wait_for_event`
- `event_types` (at least one): globs over event types, such as `session.ended` or `session.*`.
- `fields`: matchers `{path, op, value}`, all of which must hold. `path` reads the envelope first (`event_type`, `entity_kind`, `entity_id`, `workspace_id`, `session_id`, `actor`), then the payload (`payload.ended_reason`). `op` is `eq` (the default), `prefix` or `regex`; `value` is a string. A missing path never matches.
- `expr`: a Rego module (`package primer.event_filter`, a boolean `match` rule) run on the event. It may not reference the `opa` package.
- The answer is `{event: {...}}` (the envelope, stored secrets masked), `{timed_out: true, elapsed_seconds, event_types}` or `{cancelled: true, cancel_reason}`.
- Only events after the call count. An event that happened before it is not replayed.
- **Limits for a run that is not an admin's.** It may wait only on `session.*`, `workspace.*`, `canvas.*`, `llm.called`, `tool.called`, `trigger.fired` and `turn.started`, with globs that cannot match anything else (`*` and `agent.*` are refused). `regex` matchers and `expr` need an admin; `eq` and `prefix` are open to all. A wait on `canvas.*` must also name your own workspace or session with an `eq` matcher. A refusal answers `forbidden`.
- **The event types.** Sessions: `session.invoked`, `session.steered`, `session.messaged`, `session.replied`, `session.parked`, `session.resumed`, `session.ended` (payload `ended_reason`), `session.turn_failed` and `session.wake`. Workspaces: `workspace.file_changed`, `workspace.exec_started` and `workspace.exec_exited`. Also `trigger.fired` (`entity_id` is the trigger), `trigger.delivery_failed`, `turn.started`, `tool.called`, `llm.called`, `mcp.tool_called`, `approval.requested`, `approval.decided`, `graph.node_entered`, `graph.node_exited`, `collection.document_pushed`, `collection.document_deleted`, the `canvas.*` events, and `<kind>.created`, `<kind>.updated` and `<kind>.deleted` for agents, graphs, collections, documents and toolsets (`agent.created`).

### The subscribe tools
- `workspace_ext__subscribe_to_trigger` takes the trigger's `id`, never its slug. The trigger must exist and be enabled (`trigger_not_found_or_disabled` otherwise). It saves a one-shot `parked_session` subscription for this call; the next fire wakes you and deletes it ([subscriptions](triggers/subscriptions)).
- `workspace_ext__subscribe_to_channel_event` takes the `id` of an enabled `channel` trigger and an optional `event_matcher`. Every field you set must hold: `event_type` (required: `message.posted`, `command.invoked`, `component.acted`, `reaction.added`, `reaction.removed`, `message.edited`, `member.joined`, `bot.installed`, `bot.removed` or `room.created`), `surface`, `room_external_ids`, `command_name`, `mentions_bot`, `sender_roles_any`, `sender_ids_any` and `text_pattern`. Leave the matcher out to wake on any event of the trigger. An unsafe `text_pattern` answers `validation-error`.
- The answer is `{ok, fire_context, payload}` on the fire (`fire_context` has the trigger, `fired_at`, `scheduled_for` and `fire_id`, and a channel event under `event`; with no template the payload is the fire context), `{timed_out: true, elapsed_seconds, trigger_id}` at the cap, or `{cancelled: true, cancel_reason}`.
- The fire runs at no more than its owners' rank. If the trigger's owner ranks below the person who started your session, the fire is refused (`steer_outranks_fire`) and your wait runs to its cap.

## How to
1. Pick the tool from the table. For a platform event, use `system__wait_for_event`.
2. Write the narrowest filter: one event type and an `eq` matcher on the id you care about.
3. Set `timeout_seconds` when 60 minutes is too long or too short.
4. Call the tool. Your turn parks.
5. Read the answer: act on `event`, decide on `timed_out` whether to wait again, and stop on `cancelled`.

## Tool calls
### Wait for a session to end
`system__wait_for_event` on `session.ended`, matched on the session's id. Only an autonomous session ends by itself ([start a session](sessions/start-a-session)).

```json
{"toolset_id": "system", "tool_name": "wait_for_event", "arguments": {"event_types": ["session.ended"], "fields": [{"path": "session_id", "op": "eq", "value": "sess-3f9a1c8d2b7e"}], "timeout_seconds": 1800}}
```

Reference: [`system__wait_for_event`](reference/tools/system/wait_for_event)

### Wait for a trigger to fire
`trigger.fired` names the trigger in `entity_id`, and the answer is the event itself.

```json
{"toolset_id": "system", "tool_name": "wait_for_event", "arguments": {"event_types": ["trigger.fired"], "fields": [{"path": "entity_id", "op": "eq", "value": "tr-3f9a1c0b7d42"}], "timeout_seconds": 3600}}
```

### Wait for a document push (needs an admin)
`collection.document_pushed` is not open to a run that is not an admin's.

```json
{"toolset_id": "system", "tool_name": "wait_for_event", "arguments": {"event_types": ["collection.document_pushed"], "fields": [{"path": "payload.collection_id", "op": "eq", "value": "kb"}]}}
```

### Subscribe to a trigger's next fire
`workspace_ext__subscribe_to_trigger` parks until the trigger fires.

```json
{"toolset_id": "workspace_ext", "tool_name": "subscribe_to_trigger", "arguments": {"trigger_id": "tr-3f9a1c0b7d42"}}
```

Reference: [`workspace_ext__subscribe_to_trigger`](reference/tools/workspace_ext/subscribe_to_trigger)

### Wait for a command in a channel
`workspace_ext__subscribe_to_channel_event` parks until someone runs `/approve` on the channel trigger.

```json
{"toolset_id": "workspace_ext", "tool_name": "subscribe_to_channel_event", "arguments": {"trigger_id": "tr-9b2e5f1a7c3d", "event_matcher": {"event_type": "command.invoked", "command_name": "approve"}}}
```

Reference: [`workspace_ext__subscribe_to_channel_event`](reference/tools/workspace_ext/subscribe_to_channel_event)

## Gotchas
- After a subscribe tool times out or is cancelled, its one-shot subscription stays on the trigger until the next fire, which deletes it and wakes nothing.
- A wait for `session.ended` on an interactive session runs to its timeout: such a session rests between turns and never ends by itself.
- An event that happened before your call does not wake you. Check the current state first, then wait.
- A wait on `session.*` with no matcher wakes on the first event of any session. Always add an `eq` matcher.
- Deleting a trigger also deletes the waits on it. The waiting session stays parked until its cap.

## Related
[ask the user](human-in-the-loop/ask-user), [sleep and watch files](utilities/sleep-and-watch-files), [messaging other sessions](sessions/messaging-other-sessions), [subscriptions](triggers/subscriptions), [start a session](sessions/start-a-session).
