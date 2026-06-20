---
slug: channels
title: Channels - multi-platform messaging
summary: How primer routes ask_user / tool approval prompts to Slack, Telegram, and Discord, and how channel events drive chats and workspace session gates.
related: [triggers-and-subscriptions, tool-approval, yielding]
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

Channels also serve as the bridge between a running primer session
that needs human input (an `ask_user` yield, a tool-approval gate)
and the human responsible for answering. When a workspace has a
channel association, all session gates from that workspace's sessions
forward to the associated channel automatically. Channels are also
the source side for channel-kind triggers: a message in a watched
channel can fire a trigger that spins up a fresh agent.

The vocabulary is three layers. A **ChannelProvider** holds the
integration-level credentials: which platform, what tokens, what
OAuth scope. A **Channel** is a specific room within that provider
with its own per-room config. A **Workspace** can have a
`channel_association` pointing at a Channel; session gates from that
workspace forward to that channel.

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
- `channel_association: {channel_id} | null` - the single Channel
  this workspace's session gates forward to. Set at create time or
  mutated via `set_workspace_channel_association` /
  `clear_workspace_channel_association`.

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
3. The dispatcher posts the prompt to the channel associated with the
   workspace. The message carries a `Workspace: <name> · Session:
   <label>` attribution header so the human knows which workflow is
   asking.
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
`channel_association` is mutable at any time.

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

### Workspace channel association

- `system::set_workspace_channel_association` - sets
  `workspace.channel_association` to `{channel_id}`. Body:
  `workspace_id`, `channel_id`. Returns `{ok: true, workspace_id,
  channel_id}`. Overwrites any existing association.
- `system::clear_workspace_channel_association` - sets
  `workspace.channel_association` to null. Body: `workspace_id`.
  Returns `{ok: true, workspace_id}`.

After setting an association, all `ask_user`, `tool_approval`, and
`inform` gates from sessions in that workspace forward to the
associated channel. No per-gate flags; the association implies all
gates forward.

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

3. Associate the workspace:

   ```json
   {
     "tool": "system::set_workspace_channel_association",
     "arguments": {
       "workspace_id": "ws-incidents",
       "channel_id": "ch-ops-pager"
     }
   }
   ```

4. Next time a session in `ws-incidents` yields with `ask_user`, a
   Slack message appears in #ops-pager (with a `Workspace: incidents
   · Session: <label>` header) with the question. A reply resumes
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

### Workflow 3 - inspect channels and check workspace association

**Goal.** Agent wants to see which channel a workspace forwards to.

1. Get the workspace:

```json
{
  "tool": "workspaces::get_workspace",
  "arguments": {"id": "ws-target"}
}
```

Returns the workspace row; `channel_association` is either
`{"channel_id": "ch-ops-pager"}` or `null`.

2. List channels if you need to enumerate available channels:

```json
{
  "tool": "system::list_channels",
  "arguments": {"limit": 100}
}
```

## Gotchas

- **Association implies all gates forward.** There are no per-gate
  flags. If a workspace is associated with a channel, all `ask_user`,
  `tool_approval`, and `inform` gates from its sessions forward to
  that channel.
- **One channel, many workspaces.** A single channel can serve
  multiple workspaces simultaneously. Each workspace's session gates
  open a separate thread or message on the channel, attributed with
  a `Workspace: <name> · Session: <label>` header.
- **Chats and session gates coexist on the same channel.** A channel
  with `config.chats.enabled=true` handles both incoming user
  messages (chats) and session gate replies from workspace
  associations. ChannelCorrelation rows keep them separate.
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
- [triggers-and-subscriptions](triggers-and-subscriptions.md) -
  `channel` kind triggers source events from channel inboxes.
