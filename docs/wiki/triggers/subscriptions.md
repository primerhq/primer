---
title: "Subscriptions"
summary: "A subscription says what a trigger fire does: start an agent or graph session, steer a session or resume a parked one."
order: 2
tools: [trigger__list_subscriptions, trigger__get_subscription, trigger__create_subscription, trigger__update_subscription, trigger__delete_subscription]
related: [triggers/triggers, triggers/waiting-for-events, sessions/start-a-session]
---
## What it is
A subscription says what a trigger's fire does. Every enabled subscription of a trigger acts on every fire: it starts a session, steers a session, or wakes a session that waits on the trigger. Manage subscriptions with `trigger__list_subscriptions`, `trigger__get_subscription`, `trigger__create_subscription`, `trigger__update_subscription` and `trigger__delete_subscription`.

## Concepts
- **The four kinds** (`config.kind`):
  - `agent_fresh_session` (`workspace_id`, `agent_id`) starts a new session of the agent. The rendered payload is its first message. The session is autonomous: it ends after its turn.
  - `graph_fresh_session` (`workspace_id`, `graph_id`) starts a new graph session. The rendered payload must be a JSON object: it becomes the `graph_input` (`graph_input_invalid` otherwise).
  - `session_append` (`session_id`) puts the rendered payload into an existing session as a user message.
  - `parked_session` belongs to one waiting session. `workspace_ext__subscribe_to_trigger` and `workspace_ext__subscribe_to_channel_event` make it, and its fire deletes it ([waiting for events](triggers/waiting-for-events)). Never create one: the tools refuse it (`parked_session_only_from_yield`).
- **`payload_template`** is a Jinja template rendered against the fire context: `trigger_id`, `trigger_slug`, `kind`, `fired_at`, `scheduled_for` and `fire_id`. A webhook fire adds `webhook_body`, `webhook_headers`, `webhook_query` and `webhook_method`; a channel fire adds `event`. The `tojson` filter quotes a value as JSON. With no template, the payload is the whole fire context as JSON. A name the fire does not have fails that delivery (`payload_template_failed`).
- **`parallelism`** is `skip` (the default) or `queue`.
  - For the fresh-session kinds, `skip` starts nothing while a session this subscription started is still at work (`skipped_subscription_busy`); `queue` always starts one. A fire that carries a channel event never skips.
  - For `session_append`, a target with a running turn or a park is busy. `skip` drops the message (`skipped_session_busy`); `queue` holds it until the session's next turn boundary, behind any older queued message. An idle target wakes at once. A paused target keeps the message whatever the mode, and the fire does not lift the pause.
- **Channel fields.** A subscription on a `channel` trigger also has `event_matcher` and `reply_target`. These tools do not set them: create channel subscriptions with `system__create_channel_binding` ([bindings](channels/bindings)).
- **Outcome.** `last_fired_at` and `last_fire_error` (JSON: `code`, `message`, `fire_id`, `scheduled_for`) show the latest delivery; a clean one clears the error. A skip touches neither. Every failed delivery is also a `trigger.delivery_failed` event.
- **Owner and rank.** Saving a subscription (create or update) makes your run's principal its `owner`. A fresh session it starts runs as that owner, at no more than the lower of the subscription owner's and the trigger owner's current roles. A gone owner (a deleted or disabled account, a revoked or expired token) fails a fresh-session fire with `owner_unresolvable` and starts nothing. A `session_append` or `parked_session` fire may not drive a session that runs above the fire's rank: it fails with `steer_outranks_fire`.
- **Reach.** From a session outside the default workspace (prime), a config may name only your own workspace or a session in it. Any other answers `not-found`, as an unknown id does, and nothing is stored. The row records that reach (`reach`), and every fire checks it again: a target outside it fails with `target_out_of_reach`. The list leaves out a subscription whose target is outside your workspace, and get, update and delete answer `subscription_not_found` for it. A subscription whose target session is gone is out of reach too: delete it from prime. From prime, or with no calling session, every workspace is in reach.

## How to
1. Create the trigger first ([triggers](triggers/triggers)) and keep its `id`.
2. Pick the kind: a new session for every fire, or one session that hears every fire.
3. Write `payload_template` as the instruction the session acts on.
4. Pick `parallelism`: `queue` when no fire may be lost.
5. Create the subscription, test it ([fire a trigger now](triggers/fire-now)), then read `last_fired_at` and `last_fire_error`.

## Tool calls
### List a trigger's subscriptions
`trigger__list_subscriptions` returns every subscription of one trigger.

```json
{"toolset_id": "trigger", "tool_name": "list_subscriptions", "arguments": {"trigger_id": "tr-3f9a1c0b7d42"}}
```

Reference: [`trigger__list_subscriptions`](reference/tools/trigger/list_subscriptions)

### Read one subscription
`trigger__get_subscription` needs both ids.

```json
{"toolset_id": "trigger", "tool_name": "get_subscription", "arguments": {"trigger_id": "tr-3f9a1c0b7d42", "subscription_id": "sb-5e8d2a6c9b10"}}
```

Reference: [`trigger__get_subscription`](reference/tools/trigger/get_subscription)

### Start an agent session on every fire
`trigger__create_subscription` with `agent_fresh_session`.

```json
{"toolset_id": "trigger", "tool_name": "create_subscription", "arguments": {"trigger_id": "tr-3f9a1c0b7d42", "config": {"kind": "agent_fresh_session", "workspace_id": "ws-ops", "agent_id": "alert-summariser"}, "payload_template": "Summarise the overnight alerts. Fired at {{ fired_at }}.", "parallelism": "skip"}}
```

Reference: [`trigger__create_subscription`](reference/tools/trigger/create_subscription)

### Start a graph session on every webhook call
The template renders a JSON object, so the graph gets it as `graph_input`.

```json
{"toolset_id": "trigger", "tool_name": "create_subscription", "arguments": {"trigger_id": "tr-8c41d2e07a5b", "config": {"kind": "graph_fresh_session", "workspace_id": "ws-ops", "graph_id": "incident-pipeline"}, "payload_template": "{\"body\": {{ webhook_body | tojson }}}", "parallelism": "queue"}}
```

### Steer one session on every fire
`session_append` with `queue` holds a message the session cannot take yet.

```json
{"toolset_id": "trigger", "tool_name": "create_subscription", "arguments": {"trigger_id": "tr-3f9a1c0b7d42", "config": {"kind": "session_append", "session_id": "sess-3f9a1c8d2b7e"}, "payload_template": "A new build finished at {{ fired_at }}. Read reports/build.md.", "parallelism": "queue"}}
```

### Change or disable a subscription
`trigger__update_subscription` changes `payload_template`, `parallelism`, `enabled` and `description`; an explicit `null` clears the template.

```json
{"toolset_id": "trigger", "tool_name": "update_subscription", "arguments": {"trigger_id": "tr-3f9a1c0b7d42", "subscription_id": "sb-5e8d2a6c9b10", "enabled": false}}
```

Reference: [`trigger__update_subscription`](reference/tools/trigger/update_subscription)

### Delete a subscription
`trigger__delete_subscription` keeps the trigger and its other subscriptions.

```json
{"toolset_id": "trigger", "tool_name": "delete_subscription", "arguments": {"trigger_id": "tr-3f9a1c0b7d42", "subscription_id": "sb-5e8d2a6c9b10"}}
```

Reference: [`trigger__delete_subscription`](reference/tools/trigger/delete_subscription)

## Gotchas
- `scheduled_for` is the tick a clock fire is for: a `scheduled` trigger's tick (on time or caught up) or a `delayed` trigger's `fire_at`. It is null for a fire now, a webhook call and a channel event. `fired_at` is when the fire ran.
- An update cannot change `config`. To point at another target, delete the subscription and create a new one.
- An update makes you the owner and records your reach, as a create does. That is how a subscription disabled on upgrade is turned back on ([triggers](triggers/triggers)).
- `parallelism` is exactly `skip` or `queue`: any other value answers `validation-error` and nothing is stored.
- A `session_append` fire never reopens a session an operator cancelled (`skipped_session_ended`). A session that ended any other way is reopened by the message. A missing session is `skipped_session_missing`.
- The session a fire starts carries `trigger_id`, `subscription_id`, `fire_id` and `fired_at` in its `metadata`.

## Related
[triggers](triggers/triggers), [fire a trigger now](triggers/fire-now), [waiting for events](triggers/waiting-for-events), [start a session](sessions/start-a-session), [channel bindings](channels/bindings).
