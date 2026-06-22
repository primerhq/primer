---
slug: channels
title: Channels - multi-platform messaging
summary: How primer routes ask_user / tool approval prompts to Slack, Telegram, and Discord, and how channel events drive chats and workspace session gates.
related: [triggers-and-subscriptions, tool-approval, yielding, graphs]
mcp_tools:
  - system::list_channel_providers
  - system::get_channel_provider
  - system::create_channel_provider
  - system::update_channel_provider
  - system::delete_channel_provider
  - system::list_channels
  - system::get_channel
  - system::create_channel
  - system::update_channel
  - system::delete_channel
  - system::set_reply_binding
  - system::clear_reply_binding
  - system::create_channel_binding
  - system::list_channel_bindings
  - system::delete_channel_binding
---

# Channels - multi-platform messaging

## Overview

Channels let primer reach humans through their existing messaging
tools instead of forcing them into the operator console. Three
adapter implementations exist today - Slack, Telegram, and Discord -
each backed by an external app/bot integration the operator
configures via a `ChannelProvider` row. A Channel is per-room: one
Slack channel, one Discord guild channel, one Telegram chat. Its
`config.chats` block controls whether incoming messages on that room
start primer chats (with which agent, allowed switches, and output
verbosity).

A channel is one room used by two independent surfaces. **Inbound:**
a raw provider event is normalized into a `ChannelEvent`, matched
against bindings, and dispatched to an action (start a chat, run a
session, resume a parked session). **Outbound:** whatever a session
sends back (an `ask_user` yield, a tool-approval gate, an `inform`,
the final result) follows a reply binding to a channel and thread.

The inbound side rides the trigger system: a `channel`-kind trigger
is the event-source anchor, and each binding is a `Subscription` on
it that pairs an `EventMatcher` with an action and a `reply_target`.
The outbound side is the unified reply binding: a session's
`reply_binding` (set per-session by a rule's `reply_target`, else the
workspace-standing `Workspace.reply_binding`) decides where it posts.

The vocabulary is layered. A **ChannelProvider** holds the
integration-level credentials. A **Channel** is a specific room with
its own per-room config. A **channel trigger** anchors inbound events
for a provider (and optionally one room); **bindings** map matchers to
actions on it. A **Workspace** can carry a `reply_binding` pointing at
a Channel; session traffic from that workspace forwards there.

A **ChannelCorrelation** is the persistent routing record keyed on
`(channel_id, anchor)` where anchor is a thread id (Slack/Discord)
or a gate message id (Telegram). It maps to either a chat or a
pending session gate. This is the durable store that lets a single
channel serve many workspaces and standalone chats simultaneously
while still routing replies to the right target.

## Mental model

ChannelProvider:
- `id`, `provider` (`slack | telegram | discord`).
- `config` - discriminated by `provider`. For Slack:
  `{app_token, bot_token, signing_secret}`. For Telegram:
  `{bot_token, poll_timeout_seconds}`. For Discord:
  `{bot_token, enable_dms}`.
- Secrets are write-only; GET / list responses redact them.

Channel:
- `id`, `provider_id` (FK to ChannelProvider), `provider`
  (platform enum, must match provider's platform), `external_id`
  (platform's room id), `label`.
- `config` - provider-discriminated per-room config:
  `SlackChannelConfig | DiscordChannelConfig | TelegramChannelConfig`.
  Each has a `chats: ChatConfig` block.

ChatConfig (inside `config.chats`):
- `enabled` (bool, default false) - whether incoming messages on this
  room start primer chats.
- `default_agent` - agent each new chat begins with; required when
  `enabled=true`.
- `allow_agent_switch` (bool, default false) - whether users may change
  a chat's agent with `/agent`. Off by default, so enabling chats does
  not implicitly let anyone reassign the agent. `allowed_agents` only
  applies when this is on.
- `allowed_agents` (list of agent ids, default `[]`) - when agent
  switching is allowed, restricts `/agent` to these agents; empty means
  any agent is allowed. Ignored when `allow_agent_switch` is off.
- `relay_mode` (`"final"` | `"all"`, default `"final"`) - controls
  which chat messages are relayed back to the platform. `"final"`
  sends only the last assistant turn; `"all"` streams every turn.

Workspace:
- `reply_binding: {channel_id} | null` - the single Channel this
  workspace's session traffic forwards to (the standing outbound
  binding). Set at create time or mutated via `set_reply_binding` /
  `clear_reply_binding`.

ChannelEvent (normalized inbound envelope):
- `type` - `message.posted` or `command.invoked` (v1 core).
- `surface` - `dm | channel | thread`.
- `mentions_bot`, `command` (`{name, args}`), `sender`
  (`{external_id, roles, is_bot}`), `text`, `room_external_id`.

EventMatcher (the binding predicate, AND of present fields):
- `event_type` (required), `surface`, `command_name`, `mentions_bot`,
  `sender_roles_any`, `sender_ids_any`, `text_pattern` (regex),
  `room_external_ids`. Omitted fields are unconstrained.

ReplyTarget (where the action's reply goes):
- `source_thread` (default) | `source_room` | `dm_sender` | `none`,
  or an explicit `{channel_id, anchor}`. For session actions the
  resolved target becomes a session-scoped reply binding that wins
  over the workspace-standing one.

ChannelCorrelation (DB, internal):
- Keyed `(channel_id, anchor)`.
- `kind`: `"chat"` or `"session"`.
- For `kind="chat"`: `chat_id`.
- For `kind="session"`: `workspace_id`, `session_id`, `tool_call_id`
  (the currently-pending gate).
- Anchor = thread id (Slack/Discord) / gate message id (Telegram)
  / `"__active_chat__"` for single-type channels.

One channel can simultaneously serve many workspaces (session gates)
and standalone chats. Each open thread or gate has its own
ChannelCorrelation row; the inbound router resolves the anchor from
the incoming message and dispatches to the right destination.

The dispatch flow for an `ask_user`:

1. Tool returns `Yielded(tool_name="ask_user", ...)`.
2. Worker parks the session.
3. The dispatcher resolves the reply binding (session-scoped first,
   then the workspace `reply_binding`) and posts the prompt there. The
   message carries a `Workspace: <name> / Session: <label>` attribution
   header so the human knows which workflow is asking.
4. The adapter writes a `ChannelCorrelation(kind="session")` row for
   the reply anchor.

The response path:

1. Platform delivers the user's reply to primer's inbound webhook.
2. `ChannelInboundRouter.route` resolves the anchor against
   `ChannelCorrelation`.
3. For `kind="session"`: publishes `ask_user:{sid}:{tcid}` onto the
   event bus.
4. The first publish triggers `mark_resumable`; subsequent ones
   no-op via the atomic guard.

Tool approval forwarding works identically, producing a
`tool_approval:{sid}:{tcid}` event.

## Lifecycle and states

A ChannelProvider has no lifecycle beyond its config. A Channel has
no lifecycle state (config is mutable via PUT). A Workspace's
`reply_binding` is mutable at any time.

The **dispatch attempt** has these moments:

- **dispatched** - post sent, awaiting response. The session is
  parked; the user has not replied.
- **resolved by reply** - a response arrived; first one wins; session
  resumes.
- **resolved by timeout** - the yield's timeout fired; session
  resumes with `YieldTimeout`. Late channel replies produce no effect.
- **resolved by cancellation** - operator cancelled the session.
- **post failed** - the post itself errored. Logged WARN. The session
  stays parked; the user never sees the prompt. Monitor server logs.

## MCP tools

Channels are operator-config; the CRUD tools are available via the
system toolset for completeness but agents rarely need to touch them.

### ChannelProvider CRUD

- `system::list_channel_providers`
- `system::get_channel_provider`
- `system::create_channel_provider` - body needs `provider`,
  `config`, and an optional `id`. Omit `id` and the server assigns
  `channel-provider-<hex>`; supply one to use it verbatim. Secrets
  in `config` are write-only; GET / list responses redact them.
- `system::update_channel_provider`
- `system::delete_channel_provider` - cascade-blocked if any Channel
  references it.

### Channel CRUD

- `system::list_channels`, `system::get_channel`,
  `system::create_channel`, `system::update_channel`,
  `system::delete_channel`.
- `create_channel` body needs `provider_id`, `provider`,
  `external_id`, optional `label`, optional `id`, and optional
  `config`. Omit `id` and the server assigns `channel-<hex>`.
  `provider` must match the referenced provider's platform. The pair
  `(provider_id, external_id)` must be unique.
- `config.chats` controls chat enablement for the room. Set
  `config.chats.enabled=true` and `config.chats.default_agent=<id>`
  to allow incoming messages to start chats on this room.

### Workspace reply binding

- `system::set_reply_binding` - sets `workspace.reply_binding` to
  `{channel_id}`. Body: `workspace_id`, `channel_id`. Overwrites any
  existing binding.
- `system::clear_reply_binding` - sets `workspace.reply_binding` to
  null. Body: `workspace_id`.

After setting a reply binding, all `ask_user`, `tool_approval`, and
`inform` gates plus the start ack and final result from sessions in
that workspace forward to the bound channel. No per-gate flags; the
binding implies all of it forwards (a per-binding quiet mode can
suppress the acks while still forwarding gates).

### Inbound channel bindings

- `system::create_channel_binding` - create a `matcher -> action`
  binding (a Subscription) on a `channel`-kind trigger. Body:
  `trigger_id`, `event_matcher`, `config` (the action), optional
  `reply_target`, `payload_template`, `parallelism`, `enabled`. An
  unknown trigger returns `type=trigger_not_found`.
- `system::list_channel_bindings` - list the bindings on a channel
  trigger. Body: `trigger_id`.
- `system::delete_channel_binding` - remove one binding while keeping
  the trigger. Body: `trigger_id`, `subscription_id`.

The `channel` trigger itself is created with the regular trigger tools
(`trigger::create` with `config.kind="channel"` and a `provider_id`,
optional `channel_id`).

## Workflows

### Workflow 1 - operator wires up Slack for ask_user prompts

**Goal.** Forward every `ask_user` from workspace `ws-incidents` to
Slack channel #ops-pager.

1. Operator already has the Slack app installed and the OAuth flow
   complete. They create the ChannelProvider:

   ```json
   {
     "tool": "system::create_channel_provider",
     "arguments": {
       "entity": {
         "id": "cp-slack",
         "provider": "slack",
         "config": {"app_token": "xapp-...", "bot_token": "xoxb-..."}
       }
     }
   }
   ```

2. Create the Channel row for #ops-pager:

   ```json
   {
     "tool": "system::create_channel",
     "arguments": {
       "entity": {
         "id": "ch-ops-pager",
         "provider_id": "cp-slack",
         "provider": "slack",
         "external_id": "C012ABCDEF",
         "label": "#ops-pager"
       }
     }
   }
   ```

3. Bind the workspace:

   ```json
   {
     "tool": "system::set_reply_binding",
     "arguments": {
       "workspace_id": "ws-incidents",
       "channel_id": "ch-ops-pager"
     }
   }
   ```

4. Next time a session in `ws-incidents` yields with `ask_user`, a
   Slack message appears in #ops-pager (with a `Workspace: incidents
   / Session: <label>` header) with the question. A reply resumes
   the session.

### Workflow 2 - enable chats on a Telegram room

**Goal.** Incoming DMs to a Telegram bot start a chat with a
`helpdesk` agent.

1. Create the channel with chats enabled:

   ```json
   {
     "tool": "system::create_channel",
     "arguments": {
       "entity": {
         "id": "ch-tg-helpdesk",
         "provider_id": "cp-telegram",
         "provider": "telegram",
         "external_id": "987654321",
         "label": "helpdesk-dm",
         "config": {
           "chats": {
             "enabled": true,
             "default_agent": "helpdesk",
             "relay_mode": "final"
           }
         }
       }
     }
   }
   ```

2. Incoming DMs now start a chat with the `helpdesk` agent. The
   conversation persists across messages in the same Telegram chat.

### Workflow 3 - inspect channels and check the reply binding

**Goal.** Agent wants to see which channel a workspace forwards to.

1. Get the workspace:

```json
{
  "tool": "workspaces::get_workspace",
  "arguments": {"id": "ws-target"}
}
```

Returns the workspace row; `reply_binding` is either
`{"channel_id": "ch-ops-pager"}` or `null`.

2. List channels if you need to enumerate available channels:

```json
{
  "tool": "system::list_channels",
  "arguments": {"limit": 100}
}
```

### Workflow 4 - map a slash command to a fresh session

**Goal.** When someone runs `/deploy` in a Slack channel, run the
`deployer` agent and reply in the originating thread.

1. Create the `channel`-kind trigger (the event-source anchor):

```json
{
  "tool": "trigger::create",
  "arguments": {
    "slug": "slack-deploy-anchor",
    "name": "Slack deploy command",
    "config": {
      "kind": "channel",
      "provider_id": "cp-slack",
      "channel_id": "ch-deploys"
    },
    "enabled": true
  }
}
```

2. Create the binding that maps the matcher to the action. Request:

```json
{
  "tool": "system::create_channel_binding",
  "arguments": {
    "trigger_id": "slack-deploy-anchor",
    "event_matcher": {
      "event_type": "command.invoked",
      "command_name": "deploy"
    },
    "config": {
      "kind": "agent_fresh_session",
      "workspace_id": "ws-ops",
      "agent_id": "deployer"
    },
    "reply_target": "source_thread"
  }
}
```

Response (the created Subscription):

```json
{
  "id": "sub-deploy-1",
  "trigger_id": "slack-deploy-anchor",
  "event_matcher": {
    "event_type": "command.invoked",
    "command_name": "deploy"
  },
  "config": {
    "kind": "agent_fresh_session",
    "workspace_id": "ws-ops",
    "agent_id": "deployer"
  },
  "reply_target": "source_thread",
  "parallelism": "skip",
  "enabled": true
}
```

Now every `/deploy` in `ch-deploys` runs the `deployer` agent. The
`source_thread` reply target makes that session's start ack, gates,
and final result land back in the same thread.

### Workflow 5 - park a workflow on a channel event

**Goal.** A running session waits until a `command.invoked` event
matching `approve` arrives, then resumes.

```json
{
  "tool": "workspace_ext::subscribe_to_channel_event",
  "arguments": {
    "trigger_id": "slack-deploy-anchor",
    "event_matcher": {
      "event_type": "command.invoked",
      "command_name": "approve"
    }
  }
}
```

This is a yielding tool: it persists a one-shot `parked_session`
binding on the channel trigger and parks the calling session. When a
matching event fires the trigger, the session resumes with the event
in its tool result. Omit `event_matcher` to resume on any event for
the trigger.

## Gotchas

- **A reply binding implies all gates forward.** There are no per-gate
  flags. If a workspace has a `reply_binding`, all `ask_user`,
  `tool_approval`, and `inform` gates from its sessions forward to
  that channel (plus the start ack and final result, unless the
  binding is quiet).
- **Session-scoped reply binding wins over the workspace one.** A
  channel rule's `reply_target` sets a per-session binding anchored to
  the originating thread, which takes precedence over the workspace
  `reply_binding`.
- **Correlation-first inbound.** A reply that continues an existing
  conversation (a known thread with a parked session or a bound chat)
  is handled by the correlation store and never reaches the rules;
  only fresh events are matched against bindings.
- **One channel, many workspaces.** A single channel can serve
  multiple workspaces simultaneously. Each workspace's session gates
  open a separate thread or message on the channel, attributed with
  a `Workspace: <name> / Session: <label>` header.
- **Chats and session gates coexist on the same channel.** A channel
  with `config.chats.enabled=true` handles both incoming user
  messages (chats) and session gate replies from reply bindings.
  ChannelCorrelation rows keep them separate.
- **`config.chats.default_agent` is required when
  `config.chats.enabled=true`.** Omitting it returns `422`.
- **`/agent` switching is gated by `allow_agent_switch`.** It is off by
  default; while off, `/agent` (and the in-thread / modal picker)
  returns "Agent switching is disabled on this channel." Turn it on to
  let users reassign a chat's agent.
- **`allowed_agents` restricts `/agent` switching.** Applies only when
  `allow_agent_switch` is on. An empty list allows any agent; a
  non-empty list restricts `/agent <id>` to those ids only.
- **Dispatch is fire-and-forget.** A slow channel post does NOT block
  the worker's lease release. The session parks immediately.
- **First reply wins, late replies no-op.** If a Slack reply and a
  REST response arrive nearly simultaneously, primer accepts whichever
  lands first. The other is silently absorbed.
- **Failed posts produce no visible error to the user.** They WARN to
  the server log. Monitor for these errors; the human on the receiving
  end cannot tell the difference between "no prompt was sent" and
  "prompt failed to deliver".
- **No post-resolve notification.** Once a yield resolves (reply,
  timeout, cancellation), any channel button is not updated. Late
  clickers see no effect silently.
- **Secrets are redacted in GET/list responses.** Re-fetching a
  ChannelProvider and re-POSTing it would zero out the secrets.
  Updates must be partial.

## Related

- [tool-approval](tool-approval.md) - channels forward approval
  prompts via the same dispatcher.
- [yielding](yielding.md) - `ask_user` is the canonical yielding
  tool that channels forward.
- [graphs](graphs.md) - a graph that parks mid-run on an `ask_user`
  node forwards the prompt to a bound channel; the operator's reply
  there resumes the graph (graph human-in-the-loop).
- [triggers-and-subscriptions](triggers-and-subscriptions.md) -
  `channel` kind triggers source events from channel inboxes.
