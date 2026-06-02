---
slug: triggers-and-subscriptions
title: Triggers and subscriptions
summary: Event-scheduling primitive — time-based or channel-driven triggers fire payloads to chats, fresh sessions, or parked-yield tools.
related: [yielding, chats, sessions, channels]
mcp_tools:
  - system::list_triggers
  - system::get_trigger
  - system::create_trigger
  - system::update_trigger
  - system::delete_trigger
  - system::list_subscriptions
  - system::get_subscription
  - system::create_subscription
  - system::update_subscription
  - system::delete_subscription
  - trigger::list
  - trigger::get
  - trigger::create
  - trigger::update
  - trigger::delete
  - trigger::fire_now
---

# Triggers and subscriptions

## Overview

A **Trigger** is a recurring or one-shot event source in primer. A
**Subscription** ties a trigger to something that consumes its
fires — usually a chat (post the rendered payload as a user
message), a fresh agent or graph session (spin one up with the
payload as input), or a parked yielding tool (resume the agent
that's waiting). The pair is the primer answer to "wake my agent at
3am every day", "kick off a new analysis when this Slack channel
gets a message", "ping me when the cron deadline passes".

There are three trigger kinds and four subscription kinds. The
trigger kinds determine **how** firing happens; the subscription
kinds determine **what** the fire dispatches to. The cartesian
product is fully supported — any trigger can be subscribed to by
any kind of subscription.

Triggers and subscriptions are both regular CRUD entities — they
have list/get/create/update/delete tools in both the `system`
toolset (generic CRUD) and the `trigger` toolset (with
trigger-specific extras like `fire_now`). For most agent code the
`trigger::*` tools are the right ergonomic choice.

## Mental model

A `Trigger` row:
- `id` — operator-chosen identifier.
- `kind` — `delayed | scheduled | channel`. Determines the fire
  mechanism.
- `config` — discriminated union keyed by kind. For `delayed`:
  `{fire_at: <timestamp>}`. For `scheduled`: `{cron: "<5-field>",
  catch_up: "one|all|none"}`. For `channel`: `{channel_id: "...",
  filter: {...}}`.
- `payload_template` — a Jinja2 template rendered with the fire's
  context. Produces the body that subscribers receive.
- `enabled` — bool. Disabled triggers don't fire.
- `next_fire_at` — computed at create/update for `scheduled`
  triggers; used by the claim engine to know when to claim.

A `Subscription` row:
- `id` — operator-chosen.
- `trigger_id` — the parent trigger.
- `kind` — `chat_message | agent_fresh_session | graph_fresh_session
  | parked_session`. Determines the dispatch target.
- `config` — discriminated by kind. For `chat_message`:
  `{chat_id: "..."}`. For `agent_fresh_session`: `{agent_id: "...",
  workspace_id: "..."}`. For `graph_fresh_session`: `{graph_id: "...",
  workspace_id: "..."}`. For `parked_session`: dynamic — created at
  yield time by `subscribe_to_trigger`.
- `parallelism` — `skip | queue`. With `skip` (default), if the
  prior fire is still being processed, the new fire is a no-op for
  this sub. With `queue`, always fire.
- `enabled` — bool.

The fire pipeline:

1. The claim engine claims the trigger row (lease with eligibility
   `next_fire_at <= now() AND enabled = true`).
2. A worker calls `fire_trigger(trigger_id, scheduled_for)`.
3. The fire builds a context dict (the trigger metadata + any
   kind-specific fire context — channel payload, scheduled
   timestamp).
4. `payload_template` is rendered.
5. Every enabled subscription gets dispatched in parallel.
   Per-kind dispatcher:
   - `chat_message` — appends a user_message to the chat. Drain
     loop picks it up.
   - `agent_fresh_session` — creates a new session for the agent in
     the configured workspace, with the rendered payload as initial
     instruction.
   - `graph_fresh_session` — same but for a graph.
   - `parked_session` — publishes
     `subscription_matched(event_key="trigger:<trigger_id>")` to
     mark every parked session waiting on that key resumable.
6. Subscription result is logged.
7. Next `next_fire_at` is computed (for `scheduled`) and persisted.

`delayed` triggers fire once and then `next_fire_at` becomes null.
`channel` triggers don't have a `next_fire_at` — they fire when the
channel inbox notices a matching message.

## Lifecycle and states

A trigger transitions through these states implicitly via its
fields (no enum):

- **idle** — `enabled=true`, no in-flight fire. Eligible for claim
  when `next_fire_at <= now()` (scheduled/delayed) or when a
  channel event arrives.
- **firing** — claimed by a worker; fire pipeline running. Lease
  is held; next claim attempt has to wait.
- **disabled** — `enabled=false`. Not claimable.

A subscription's state is simpler: enabled or disabled. The
`parked_session` kind has a one-shot lifecycle — created when a
yielding tool calls `subscribe_to_trigger`, deleted by the worker
once it has fired.

The fire pipeline guarantees **at most once** per
`(trigger_id, scheduled_for)` tuple — the `FOR UPDATE SKIP LOCKED`
on the lease means no two workers fire the same scheduled instance.
Catch-up policy for scheduled triggers handles what happens when
the server was down across a fire window:

- `catch_up: one` (default) — fire once, with the most recent
  missed timestamp. Then schedule the next fire normally.
- `catch_up: all` — fire once per missed instance, in order. Useful
  for triggers where each instance has meaningful state (don't skip
  a daily report just because the server was down).
- `catch_up: none` — skip missed fires entirely; schedule next fire
  for the next future instance.

## MCP tools

The two surfaces are equivalent for most operations. Use
`trigger::*` when the agent thinks of triggers as a primary
abstraction (creating them, firing them); use `system::*` when the
agent is doing generic entity CRUD.

### Trigger management (use `trigger::*`)

- `trigger::list` — paginated listing. Same shape as system list.
- `trigger::get` — fetch by id.
- `trigger::create` — body needs `id`, `kind`, `config`,
  `payload_template`, optional `enabled`. Validates the cron
  expression / fire_at timestamp at create time.
- `trigger::update` — partial update. Editing `cron` recomputes
  `next_fire_at`. Editing kind is rejected.
- `trigger::delete` — cascade-deletes subscriptions.
- `trigger::fire_now` — manual fire, bypassing schedule. Useful
  for testing and on-demand kicks. Increments `last_fired_at` and
  runs the normal subscription dispatch.

### Subscription management

Use the system toolset's generic CRUD: `system::create_subscription`,
`system::list_subscriptions`, etc. Bodies require `trigger_id` and
the kind-specific `config`.

### Yielding wait (not MCP-exposable)

`trigger::subscribe_to_trigger` is a yielding tool — invisible from
MCP. For external agents that want event-driven behaviour, poll
`system::list_subscriptions(trigger_id=X)` or inspect the trigger's
`last_fired_at` to detect fires.

## Workflows

### Workflow 1 — schedule a daily summary

**Goal.** Every weekday at 9am, fire a freshly-instantiated session
of the `summarise-overnight-alerts` agent in workspace `ws-ops`.

1. Create the trigger:

```json
{
  "tool": "trigger::create",
  "arguments": {
    "id": "tg-morning-summary",
    "kind": "scheduled",
    "config": {"cron": "0 9 * * 1-5", "catch_up": "one"},
    "payload_template": "Summarise overnight alerts from {{ scheduled_for }}.",
    "enabled": true
  }
}
```

2. Create the subscription:

```json
{
  "tool": "system::create_subscription",
  "arguments": {
    "id": "sub-morning-summary",
    "trigger_id": "tg-morning-summary",
    "kind": "agent_fresh_session",
    "config": {
      "agent_id": "summarise-overnight-alerts",
      "workspace_id": "ws-ops"
    },
    "enabled": true
  }
}
```

3. To test before tomorrow morning:

```json
{
  "tool": "trigger::fire_now",
  "arguments": {"id": "tg-morning-summary"}
}
```

A new session appears in `ws-ops` with the rendered payload as
initial instruction.

### Workflow 2 — wait for a channel event

**Goal.** When a particular Slack channel receives a message, kick
off the `triage-incident` agent.

1. Create the trigger:

```json
{
  "tool": "trigger::create",
  "arguments": {
    "id": "tg-slack-incident",
    "kind": "channel",
    "config": {
      "channel_id": "ch-slack-oncall",
      "filter": {"text_contains": "incident"}
    },
    "payload_template": "{{ channel_message.text }}",
    "enabled": true
  }
}
```

2. Subscribe a fresh `triage-incident` session per fire:

```json
{
  "tool": "system::create_subscription",
  "arguments": {
    "id": "sub-slack-incident",
    "trigger_id": "tg-slack-incident",
    "kind": "agent_fresh_session",
    "config": {
      "agent_id": "triage-incident",
      "workspace_id": "ws-incidents"
    },
    "parallelism": "queue",
    "enabled": true
  }
}
```

`parallelism: queue` means every matching message produces its own
session even if a prior triage is still running.

## Gotchas

- **Per-subscription parallelism is independent of trigger
  fires.** A trigger always fires when its conditions are met. But
  each subscription decides whether to act on that fire. `skip`
  drops the fire for that sub if its prior invocation is still
  running; `queue` always acts. So a high-frequency trigger with
  `skip` subs effectively rate-limits per-sub.
- **`parked_session` subscriptions are dynamic and one-shot.** They
  exist only between the moment a `subscribe_to_trigger` yields
  and the moment the trigger fires (or the timeout sweeper times
  the yield out). Don't try to CRUD them — they appear and
  disappear with the yielding tool.
- **Catch-up policy matters after downtime.** A nightly trigger
  with `catch_up: all` will fire 5 times when the server starts
  after a 5-day outage. Operators tuning recovery should know
  which policy each trigger uses.
- **`payload_template` runs Jinja2 against a fire-time context.**
  The context dict varies by kind: scheduled gets `scheduled_for`,
  channel gets `channel_message`, delayed gets nothing beyond the
  trigger metadata. Template errors are per-sub: a bad template
  fails this one sub without taking down the others.
- **`channel` triggers depend on the `ChannelInbox`.** No
  ChannelProvider configured + no Channel rows → channel triggers
  never fire. Operators see this as "trigger created but never
  fires" and it's confusing.
- **`trigger::fire_now` is synchronous from the operator's side**
  (returns when the fire dispatch starts) but the subscribers act
  asynchronously. The endpoint returns 202; check `last_fired_at`
  to confirm the fire actually ran.
- **`subscribe_to_trigger` is invisible from MCP.** External agents
  must poll. The `trigger::list` tool gives them the trigger's
  metadata; `system::list_subscriptions(trigger_id=X)` lets them
  observe state changes.
- **Triggers and subscriptions are claimed via the same engine as
  sessions and chats.** A backed-up worker pool can delay trigger
  fires; this is rare in healthy systems but worth knowing when
  diagnosing "my 9am trigger fired at 9:03am".

## Related

- [yielding](yielding.md) — `subscribe_to_trigger` is the
  yielding tool that parks on trigger fire events.
- [chats](chats.md) — `chat_message` subscriptions append to
  chats.
- [sessions](sessions.md) — `agent_fresh_session` and
  `graph_fresh_session` subscriptions spin up new sessions.
- [channels](channels.md) — `channel` triggers source events from
  channel inboxes.
