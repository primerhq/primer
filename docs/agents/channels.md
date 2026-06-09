---
slug: channels
title: Channels - multi-platform messaging
summary: How primer routes ask_user / tool approval prompts to Slack, Telegram, and Discord, and how channel events fire triggers.
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
  - system::list_workspace_channel_associations
  - system::create_workspace_channel_association
  - system::delete_workspace_channel_association
---

# Channels - multi-platform messaging

## Overview

Channels let primer reach humans through their existing messaging
tools instead of forcing them into the operator console. Three
adapter implementations exist today - Slack, Telegram, and Discord -
each backed by an external app/bot integration the operator
configures via a `ChannelProvider` row. Channels are the bridge
between a running primer session that needs human input (an
`ask_user` yield, a `_approval` gate) and the human responsible for
answering. They're also the source side for channel-kind triggers:
a message in a watched channel can fire a trigger that spins up a
fresh agent.

The vocabulary is three layers. A **ChannelProvider** is the
integration-level config: which platform, what credentials, what OAuth
scope. A **Channel** is a specific account or workspace within that
platform (one Slack workspace, one Discord guild, one Telegram chat).
A **WorkspaceChannelAssociation** binds a primer Workspace to a
Channel and sets two forwarding flags: `forward_ask_user` and
`forward_tool_approval`. Both flags default off; the operator opts in
per association.

The fanout pattern is fire-and-forget: when a session yields with
`ask_user` or hits an approval gate, the `ChannelDispatcher` posts to
every enabled association in parallel. Posts that fail (Slack outage,
Telegram rate limit, Discord interaction expiry) log a WARN but don't
block the worker - the session has already parked. Conversely, the
**first response wins**: whichever platform's reply lands first in the
`ChannelInbox` publishes a `subscription_matched` event and the session
resumes. Late responses from other platforms are accepted but produce
no effect (the scheduler sees the session already resumable and the
publish is a no-op).

## Mental model

ChannelProvider:
- `id`, `provider_type` (`slack | telegram | discord`).
- `config` - discriminated by `provider_type`. For Slack:
  `{client_id, client_secret, signing_secret, bot_token, oauth_token,
  workspace_id}`. For Telegram: `{bot_token}`. For Discord:
  `{app_id, public_key, bot_token}`.

Channel:
- `id`, `provider_id` (FK to ChannelProvider).
- `external_id` - the platform's id for this account (Slack
  channel id, Discord guild id, Telegram chat id).
- `label` - operator-facing name.

WorkspaceChannelAssociation:
- `id`, `workspace_id`, `channel_id`.
- `forward_ask_user` (bool), `forward_tool_approval` (bool).
- `enabled` (bool) - toggle the whole association without deleting.

The dispatch flow for an `ask_user`:

1. Tool returns `Yielded(tool_name="ask_user", event_key="ask_user:<sid>:<tcid>", ...)`.
2. Worker parks the session.
3. ChannelDispatcher (separate from the worker) is asked to dispatch
   the prompt. It queries the workspace's associations with
   `forward_ask_user=true AND enabled=true`.
4. For each association, the per-platform adapter's `post_prompt()`
   constructs the message (with a callback id round-trip token) and
   posts to the platform's API.
5. Posts run in parallel; failures don't block others.

The response path:

1. Platform delivers the user's reply to primer's inbound webhook.
2. `ChannelInbox.handle_response(envelope)` decodes the round-trip
   token to recover `(workspace_id, session_id, tcid)`.
3. The inbox publishes `subscription_matched(event_key="ask_user:<sid>:<tcid>")`.
4. The first publish marks the session resumable; subsequent ones
   no-op via the `mark_resumable` atomic guard.

Tool approval forwarding works identically with `event_key=
"tool_approval:<sid>:<tcid>"`.

The round-trip identifier mechanism varies per platform. Slack uses
the `value` field of an interactive button. Telegram uses
`callback_data`. Discord uses `custom_id`. All three are encoded the
same way: an 8-byte digest of `(workspace_id, session_id, tcid)`
plus the explicit ids. The inbox has a process-local cache mapping
digest → ids; cache miss falls back to a storage scan. The cache is
an optimisation, not truth.

## Lifecycle and states

A ChannelProvider has no lifecycle beyond its config. A Channel
the same. A WorkspaceChannelAssociation has `enabled`. The
**dispatch attempt** has these moments:

- **dispatched** - post sent, awaiting response. The session is
  parked; the user hasn't replied.
- **resolved by reply** - a response arrived; first one wins; session
  resumes.
- **resolved by timeout** - the yield's timeout fired; the session
  resumes with `YieldTimeout`. Late channel replies still come in
  but produce no effect.
- **resolved by cancellation** - operator cancelled the session;
  yield cancellation runs. Late channel replies again do nothing.
- **post failed** - the post itself errored. Logged WARN. The
  session is still parked; if no other platform succeeded, the user
  will never see the prompt. This is a serious UX bug from the
  human's perspective; monitor logs.

There's no "post-resolve" hook to tell channels "this prompt was
answered, please disable the button" - late clickers just see no
effect.

## MCP tools

Channels are operator-config; the CRUD tools are available via the
system toolset for completeness but agents rarely need to touch
them.

### ChannelProvider CRUD

- `system::list_channel_providers`
- `system::get_channel_provider`
- `system::create_channel_provider` - body needs `provider_type`,
  `config`, and an optional `id`. Omit `id` and the server assigns
  `channel-provider-<hex>` (e.g. `channel-provider-3f9a1c8d`);
  supply one to use it verbatim. Immutable after creation. Secrets
  in `config` are write-only; GET / list responses redact them.
- `system::update_channel_provider`
- `system::delete_channel_provider` - cascade-blocked if any
  Channel references it.

### Channel CRUD

- `system::list_channels`, `system::get_channel`,
  `system::create_channel`, `system::update_channel`,
  `system::delete_channel`.
- `create_channel` body needs `provider_id`, `external_id`,
  `label`, and an optional `id`. Omit `id` and the server assigns
  `channel-<hex>` (e.g. `channel-3f9a1c8d`); supply one to use it
  verbatim. Immutable after creation. External_id must match the
  platform's actual id.

### Association CRUD

- `system::list_workspace_channel_associations`
- `system::create_workspace_channel_association` - body needs
  `workspace_id`, `channel_id`, `forward_ask_user`,
  `forward_tool_approval`, `enabled`, and an optional `id`. Omit
  `id` and the server assigns
  `workspace-channel-association-<hex>`; supply one to use it
  verbatim. Immutable after creation.
- `system::delete_workspace_channel_association` - instant; the
  next dispatch sees no row for this association.

## Workflows

### Workflow 1 - operator wires up Slack for ask_user prompts

**Goal.** Forward every `ask_user` from workspace `ws-incidents` to
Slack channel #ops-pager.

1. Operator already has the Slack app installed and the OAuth flow
   complete. They create the ChannelProvider via the console:

   ```
   POST /v1/channel_providers
   { "id": "cp-slack", "provider_type": "slack", "config": {...creds...} }
   ```

2. Create the Channel row for #ops-pager:

   ```json
   {
     "tool": "system::create_channel",
     "arguments": {
       "id": "ch-ops-pager",
       "provider_id": "cp-slack",
       "external_id": "C012ABCDEF",
       "label": "#ops-pager"
     }
   }
   ```

3. Associate the workspace:

   ```json
   {
     "tool": "system::create_workspace_channel_association",
     "arguments": {
       "id": "wca-ws-incidents-ops-pager",
       "workspace_id": "ws-incidents",
       "channel_id": "ch-ops-pager",
       "forward_ask_user": true,
       "forward_tool_approval": false,
       "enabled": true
     }
   }
   ```

4. Next time a session in `ws-incidents` yields with `ask_user`, a
   Slack message appears in #ops-pager with the question. A reply
   resumes the session.

### Workflow 2 - agent observes a channel-driven trigger

**Goal.** Agent wants to know whether a channel is wired up to fire
triggers. It can inspect the channel surface.

1. List channels:

```json
{
  "tool": "system::list_channels",
  "arguments": {"limit": 100}
}
```

Returns `[{"id": "ch-ops-pager", "provider_id": "cp-slack", ...}]`.

2. List associated workspaces:

```json
{
  "tool": "system::list_workspace_channel_associations",
  "arguments": {"limit": 100}
}
```

3. The agent infers (or asks the user) which channels are bound to
   triggers it cares about. Triggers themselves are inspected via
   `trigger::list` and `trigger::get`.

## Gotchas

- **`enabled=false` skips both forward flags.** Disabling an
  association silences both `ask_user` and tool-approval forwarding
  without distinguishing - there's no per-flag enable. Use the two
  forward flags to choose; use `enabled` to mute the whole link.
- **Dispatch is fire-and-forget.** A slow channel post does NOT
  block the worker's lease release. The session parks immediately.
- **First reply wins, late replies no-op.** If you receive a Slack
  ack and a Discord ack at nearly the same instant, primer accepts
  whichever landed first. The "loser" platform's reply is logged
  but produces no state change.
- **Failed posts produce no visible error.** They WARN to the
  server log. Operators relying on Slack forwards need to monitor
  for these errors - the user/operator on the receiving end can't
  tell the difference between "no prompt was sent" and "prompt
  failed to deliver".
- **No post-resolve notification.** Once a yield resolves (reply,
  timeout, cancellation), the channel button isn't updated. Late
  clickers see "no response" silently. Don't promise users
  "your click was registered" - they may have clicked after
  resolution.
- **The platform round-trip id is opaque.** Slack `value`, Discord
  `custom_id`, Telegram `callback_data` are all primer-encoded
  payloads. Don't try to parse them outside primer's inbox.
- **Secrets are redacted in GET/list responses.** Re-fetching a
  ChannelProvider row and re-POSTing it would zero out the secrets.
  Updates must be partial - only fields you actually want to change.
- **Sub-spec adapters lag the core spec.** The Slack / Telegram /
  Discord implementations may have feature gaps relative to the
  core dispatcher (e.g. interactive components on Telegram are
  limited to inline keyboards). Test the actual reply UX in each
  platform before relying on it.

## Related

- [tool-approval](tool-approval.md) - channels forward approval
  prompts via the same dispatcher.
- [yielding](yielding.md) - `ask_user` is the canonical yielding
  tool that channels forward.
- [triggers-and-subscriptions](triggers-and-subscriptions.md) -
  `channel` kind triggers source events from channel inboxes.
