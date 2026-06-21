---
slug: triggers-and-subscriptions
title: Triggers and subscriptions
summary: Event-scheduling primitive - time-based, webhook, or channel-driven triggers fire payloads to chats, fresh sessions, or parked-yield tools.
related: [yielding, chats, sessions, channels]
mcp_tools:
  - trigger::list
  - trigger::get
  - trigger::create
  - trigger::update
  - trigger::delete
  - trigger::fire_now
  - trigger::list_subscriptions
  - trigger::get_subscription
  - trigger::create_subscription
  - trigger::update_subscription
  - trigger::delete_subscription
---

# Triggers and subscriptions

## Overview

A **Trigger** is a recurring or one-shot event source in primer. A
**Subscription** ties a trigger to something that consumes its
fires - usually a chat (post the rendered payload as a user
message), a fresh agent or graph session (spin one up with the
payload as input), or a parked yielding tool (resume the agent
that's waiting). The pair is the primer answer to "wake my agent at
3am every day", "kick off a new analysis when this Slack channel
gets a message", "ping me when the cron deadline passes".

There are four trigger kinds and five subscription kinds. The
trigger kinds determine **how** firing happens; the subscription
kinds determine **what** the fire dispatches to. The cartesian
product is fully supported - any trigger kind can be subscribed to
by any subscription kind.

**Trigger kinds at a glance:**
- `delayed` - one-shot, fires at a UTC timestamp.
- `scheduled` - recurring cron expression with timezone and catch-up policy.
- `webhook` - event-driven; fires when an authenticated HTTP POST arrives
  at the generated `POST /v1/webhooks/{token}` URL (no auth required on
  that endpoint; the token is the credential). The fire context includes
  `webhook_body`, `webhook_headers`, `webhook_query`, and `webhook_method`
  so payload templates can forward the inbound data to agents.

Triggers and subscriptions are both regular CRUD entities - they
have list/get/create/update/delete tools in both the `system`
toolset (generic CRUD) and the `trigger` toolset (with
trigger-specific extras like `fire_now`). For most agent code the
`trigger::*` tools are the right ergonomic choice.

## Mental model

A `Trigger` row:
- `id` - stable identifier (auto-generated if omitted).
- `slug` - human-friendly unique id (required on create); `name` -
  display name (required on create).
- `config` - discriminated union keyed by an inner `kind`. For
  `delayed`: `{kind: "delayed", fire_at: <timestamp>}`. For
  `scheduled`: `{kind: "scheduled", cron: "<5-field>", timezone:
  "UTC", catchup: "one|all|none"}`. For `webhook`: `{kind: "webhook",
  token: "<32-hex-server-minted>", hmac_secret: "<optional>"}`. For
  `channel`: `{kind: "channel", provider_id: "...", channel_id:
  "<optional>"}` - the event-source anchor (omit `channel_id` for a
  provider-wide trigger).
- `enabled` - bool. Disabled triggers don't fire.
- `next_fire_at` - computed at create/update for `scheduled`
  triggers; used by the claim engine to know when to claim.

Note: the `payload_template` lives on the **subscription**, not the
trigger - each subscription renders its own body from the fire
context (see the `Subscription` row below).

A `Subscription` row:
- `id` - operator-chosen.
- `trigger_id` - the parent trigger.
- `kind` - `chat_message | agent_fresh_session | graph_fresh_session
  | parked_session | start_chat`. Determines the dispatch target.
- `config` - discriminated by kind. For `chat_message`:
  `{chat_id: "..."}`. For `agent_fresh_session`: `{agent_id: "...",
  workspace_id: "..."}`. For `graph_fresh_session`: `{graph_id: "...",
  workspace_id: "..."}`. For `start_chat`: `{agent_id: "..."}` (opens
  a fresh chat with that agent; used by channel rules). For
  `parked_session`: dynamic - created at yield time by
  `subscribe_to_trigger`.
- `payload_template` - optional Jinja2 template rendered against the
  fire context; the rendered text is what this subscription dispatches
  (the chat user_message, the session's initial instruction, etc.).
- `event_matcher` - optional `EventMatcher` (channel triggers only).
  When present, only channel events that match the predicate dispatch
  this subscription; `None` (the default, and the only option for
  time/webhook triggers) means every fire dispatches.
- `reply_target` - optional `ReplyTarget` (channel triggers only).
  `source_thread` (default) | `source_room` | `dm_sender` | `none`,
  or an explicit `{channel_id, anchor}`. For session subscriptions the
  resolved target becomes the session's reply binding.
- `parallelism` - `skip | queue`. With `skip` (default), if the
  prior fire is still being processed, the new fire is a no-op for
  this sub. With `queue`, always fire.
- `enabled` - bool.

### The `channel` trigger kind

A `channel` trigger is the event-source anchor that channel
subscriptions attach to. Its config is
`ChannelTriggerConfig{provider_id, channel_id?}`: name the provider,
and optionally one room (omit `channel_id` for a provider-wide
trigger). Like webhook triggers it is not claim-engine-driven (no
`next_fire_at`); it fires when the inbound channel router receives a
normalized event for the matching `(provider_id, channel_id)`.

Bindings on a channel trigger add two fields a time/webhook trigger
does not use: `event_matcher` gates which normalized event dispatches
the subscription (an AND of present fields over event type, surface,
command name, sender, and text pattern), and `reply_target` sets where
the action's outbound traffic goes. See the channels doc for the full
event taxonomy, the matcher dimensions, and the action set; create
bindings with `system::create_channel_binding`, or park a session on a
channel event with `workspace_ext::subscribe_to_channel_event`.

The fire pipeline:

1. The claim engine claims the trigger row (lease with eligibility
   `next_fire_at <= now() AND enabled = true`).
2. A worker calls `fire_trigger(trigger_id, scheduled_for)`.
3. The fire builds a context dict (the trigger metadata + any
   kind-specific fire context - channel payload, scheduled
   timestamp).
4. Every enabled subscription gets dispatched in parallel. Each
   subscription renders its own `payload_template` against the fire
   context, then runs its per-kind dispatcher:
   - `chat_message` - appends a user_message to the chat. Drain
     loop picks it up.
   - `agent_fresh_session` - creates a new session for the agent in
     the configured workspace, with the rendered payload as initial
     instruction.
   - `graph_fresh_session` - same but for a graph.
   - `parked_session` - publishes
     `subscription_matched(event_key="trigger:<trigger_id>")` to
     mark every parked session waiting on that key resumable.
6. Subscription result is logged.
7. Next `next_fire_at` is computed (for `scheduled`) and persisted.

`delayed` triggers fire once and then `next_fire_at` becomes null.
`channel` triggers don't have a `next_fire_at` - they fire when the
channel inbox notices a matching message.

## Lifecycle and states

A trigger transitions through these states implicitly via its
fields (no enum):

- **idle** - `enabled=true`, no in-flight fire. Eligible for claim
  when `next_fire_at <= now()` (scheduled/delayed) or when a
  channel event arrives.
- **firing** - claimed by a worker; fire pipeline running. Lease
  is held; next claim attempt has to wait.
- **disabled** - `enabled=false`. Not claimable.

A subscription's state is simpler: enabled or disabled. The
`parked_session` kind has a one-shot lifecycle - created when a
yielding tool calls `subscribe_to_trigger`, deleted by the worker
once it has fired.

The fire pipeline guarantees **at most once** per
`(trigger_id, scheduled_for)` tuple - the `FOR UPDATE SKIP LOCKED`
on the lease means no two workers fire the same scheduled instance.
Catch-up policy for scheduled triggers handles what happens when
the server was down across a fire window:

- `catchup: one` (default) - fire once, with the most recent
  missed timestamp. Then schedule the next fire normally.
- `catchup: all` - fire once per missed instance, in order. Useful
  for triggers where each instance has meaningful state (don't skip
  a daily report just because the server was down).
- `catchup: none` - skip missed fires entirely; schedule next fire
  for the next future instance.

## MCP tools

The two surfaces are equivalent for most operations. Use
`trigger::*` when the agent thinks of triggers as a primary
abstraction (creating them, firing them); use `system::*` when the
agent is doing generic entity CRUD.

### Trigger management (use `trigger::*`)

- `trigger::list` - paginated listing. Same shape as system list.
- `trigger::get` - fetch by id.
- `trigger::create` - body needs `slug`, `name`, `config` (the
  `kind` is the discriminator inside `config`), optional `description`
  and `enabled`. Validates the cron expression / fire_at timestamp at
  create time. (`payload_template` is set per subscription, not here.)
- `trigger::update` - partial update. Editing `cron` recomputes
  `next_fire_at`. Editing kind is rejected.
- `trigger::delete` - cascade-deletes subscriptions.
- `trigger::fire_now` - manual fire, bypassing schedule. Useful
  for testing and on-demand kicks. Increments `last_fired_at` and
  runs the normal subscription dispatch.

### Subscription management

Use the `trigger` toolset's subscription tools: `trigger::create_subscription`,
`trigger::list_subscriptions`, etc. Bodies require `trigger_id` and
the kind-specific `config`.

### Yielding wait (not MCP-exposable)

`trigger::subscribe_to_trigger` is a yielding tool - invisible from
MCP. For external agents that want event-driven behaviour, poll
`trigger::list_subscriptions(trigger_id=X)` or inspect the trigger's
`last_fired_at` to detect fires.

## Workflows

### Workflow 1 - schedule a daily summary

**Goal.** Every weekday at 9am, fire a freshly-instantiated session
of the `summarise-overnight-alerts` agent in workspace `ws-ops`.

1. Create the trigger:

```json
{
  "tool": "trigger::create",
  "arguments": {
    "slug": "tg-morning-summary",
    "name": "Morning alert summary",
    "config": {"kind": "scheduled", "cron": "0 9 * * 1-5", "catchup": "one"},
    "enabled": true
  }
}
```

2. Create the subscription:

```json
{
  "tool": "trigger::create_subscription",
  "arguments": {
    "trigger_id": "tg-morning-summary",
    "config": {
      "kind": "agent_fresh_session",
      "agent_id": "summarise-overnight-alerts",
      "workspace_id": "ws-ops"
    },
    "payload_template": "Summarise overnight alerts from {{ scheduled_for }}.",
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

### Workflow 2 - wait for a channel event

**Goal.** When a particular Slack channel receives a message, kick
off the `triage-incident` agent.

1. Create the channel trigger (the event-source anchor):

```json
{
  "tool": "trigger::create",
  "arguments": {
    "slug": "slack-incident-anchor",
    "name": "Slack incident channel",
    "config": {
      "kind": "channel",
      "provider_id": "cp-slack",
      "channel_id": "ch-slack-oncall"
    },
    "enabled": true
  }
}
```

2. Create the binding: a matcher for messages containing "incident"
   to a fresh `triage-incident` session per fire:

```json
{
  "tool": "system::create_channel_binding",
  "arguments": {
    "trigger_id": "slack-incident-anchor",
    "event_matcher": {
      "event_type": "message.posted",
      "text_pattern": "incident"
    },
    "config": {
      "kind": "agent_fresh_session",
      "agent_id": "triage-incident",
      "workspace_id": "ws-incidents"
    },
    "reply_target": "source_thread",
    "payload_template": "{{ event.text }}",
    "parallelism": "queue"
  }
}
```

`parallelism: queue` means every matching message produces its own
session even if a prior triage is still running. The fire context
carries the firing event under `event`, so the payload template can
reference `event.text`.

### Workflow 3 - inbound webhook fires a fresh agent session

**Goal.** Any HTTP client can POST to a URL and trigger the `process-event` agent.

1. Create the trigger (no `token` needed - the server mints one):

```json
{
  "tool": "trigger::create",
  "arguments": {
    "slug": "process-event-hook",
    "name": "Process event webhook",
    "config": {"kind": "webhook"},
    "enabled": true
  }
}
```

The response includes `config.token` (32 hex chars). Build the inbound URL:
`POST https://your-primer-host/v1/webhooks/{config.token}`

2. Subscribe a fresh session per fire (use `webhook_body` in the template):

```json
{
  "tool": "trigger::create_subscription",
  "arguments": {
    "trigger_id": "<id from step 1>",
    "config": {
      "kind": "agent_fresh_session",
      "agent_id": "process-event",
      "workspace_id": "ws-main"
    },
    "payload_template": "Process this event payload: {{ webhook_body }}",
    "parallelism": "queue",
    "enabled": true
  }
}
```

3. Send an inbound webhook (from any HTTP client, no auth):

```bash
curl -X POST https://your-primer-host/v1/webhooks/{token} \
  -H "Content-Type: application/json" \
  -d '{"event": "order.placed", "order_id": "123"}'
# Returns: {"delivery_id": "fire-...", "status": "accepted"}
```

4. To add HMAC verification, update the trigger:

```json
{
  "tool": "trigger::update",
  "arguments": {
    "id": "<trigger id>",
    "config": {"kind": "webhook", "hmac_secret": "my-strong-secret"}
  }
}
```

Callers must then include `X-Primer-Signature: sha256=<hmac-sha256-hex>`.
To rotate the token: `POST /v1/triggers/{id}/rotate_token` (the old URL stops working immediately).

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
  the yield out). Don't try to CRUD them - they appear and
  disappear with the yielding tool.
- **Catch-up policy matters after downtime.** A nightly trigger
  with `catchup: all` will fire 5 times when the server starts
  after a 5-day outage. Operators tuning recovery should know
  which policy each trigger uses.
- **`payload_template` runs Jinja2 against a fire-time context.**
  The context dict varies by kind: scheduled gets `scheduled_for`,
  channel gets the firing `event` (the normalized `ChannelEvent` as a
  dict), delayed gets nothing beyond the trigger metadata, webhook
  gets `webhook_body`, `webhook_headers`, `webhook_query`, and
  `webhook_method`. Template errors are per-sub: a bad template fails
  this one sub without taking down the others.
- **`channel` triggers fire from the inbound router, not the claim
  engine.** No ChannelProvider configured plus no Channel rows means
  channel triggers never fire. A reply that continues an existing
  conversation is handled correlation-first and never reaches the
  rules; only fresh events are matched.
- **`trigger::fire_now` is synchronous from the operator's side**
  (returns when the fire dispatch starts) but the subscribers act
  asynchronously. The endpoint returns 202; check `last_fired_at`
  to confirm the fire actually ran.
- **`subscribe_to_trigger` is invisible from MCP.** External agents
  must poll. The `trigger::list` tool gives them the trigger's
  metadata; `trigger::list_subscriptions(trigger_id=X)` lets them
  observe state changes.
- **Triggers and subscriptions are claimed via the same engine as
  sessions and chats.** A backed-up worker pool can delay trigger
  fires; this is rare in healthy systems but worth knowing when
  diagnosing "my 9am trigger fired at 9:03am".

## Related

- [yielding](yielding.md) - `subscribe_to_trigger` is the
  yielding tool that parks on trigger fire events.
- [chats](chats.md) - `chat_message` subscriptions append to
  chats.
- [sessions](sessions.md) - `agent_fresh_session` and
  `graph_fresh_session` subscriptions spin up new sessions.
- [channels](channels.md) - `channel` triggers source events from
  channel inboxes.
