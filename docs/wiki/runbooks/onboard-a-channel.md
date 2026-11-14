---
title: Onboard a channel
summary: Establish an owned room, approve its channel trigger, bind inbound work and replies, then prove a real message and answer.
audience: admin
tools: [system__list_channel_providers, system__create_channel_provider, system__get_channel_provider, workspaces__get_workspace, system__get_agent, system__describe_tool, system__create_channel, system__get_channel, trigger__create, trigger__get, trigger__list, trigger__fire_now, system__create_channel_binding, system__list_channel_bindings, system__set_reply_binding, workspaces__list_workspace_sessions, workspaces__get_workspace_session, system__delete_channel_binding, system__clear_reply_binding, trigger__delete]
related: [channels, channels/bindings, triggers/triggers, runbooks/create-an-agent, runbooks/manage-a-workspace]
---
## Goal

Connect an authorized messaging room to an agent in a separate workspace, and verify the real inbound message and outbound answer.

## Preconditions

- An admin lists [`system__list_channel_providers`](reference/tools/system/list_channel_providers) and reads the intended provider with [`system__get_channel_provider`](reference/tools/system/get_channel_provider). An absent provider or missing real credentials is a stop: ask an admin to configure a provider using [`system__create_channel_provider`](reference/tools/system/create_channel_provider) with privately supplied credentials. A user cannot perform provider CRUD. Credentials are deliberately absent from this runbook; masked reads are not reusable secrets or a successful connectivity check.

```json
{"toolset_id": "system", "tool_name": "list_channel_providers", "arguments": {"limit": 200}}
```

```json
{"toolset_id": "system", "tool_name": "get_channel_provider", "arguments": {"id": "team-slack"}}
```

- The worked provider is Slack only after the admin confirms its actual id, installed app, room access and your provider-use grant. The app needs Socket Mode, `chat:write`, `channels:read` and `channels:history`, message.channels/app_mention subscriptions, and invitation to the chosen room. Confirm the real room id, not its display name. Telegram/Discord use different configs and prerequisites; do not send Slack fields to them. With no usable provider, stop rather than creating guessed rows.
- Read the prepared non-default workspace and responder agent using [`workspaces__get_workspace`](reference/tools/workspaces/get_workspace) and [`system__get_agent`](reference/tools/system/get_agent). You need full workspace access and use of the responder's model/tools. Create a missing workspace through [manage a workspace](runbooks/manage-a-workspace) or a missing responder through [create an agent](runbooks/create-an-agent). Use the returned ids throughout; the example ids and Slack room C042TEAM must be confirmed before any write.

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace", "arguments": {"id": "team-work"}}
```

```json
{"toolset_id": "system", "tool_name": "get_agent", "arguments": {"id": "team-helper"}}
```

- Inspect live policies with [`system__describe_tool`](reference/tools/system/describe_tool). Trigger create/delete ship required gates; the separate channel subscription and workspace reply-binding tools ship no gate. An operator can retune those policies, so read the exact tool before acting.

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "trigger", "tool_name": "create"}}
```

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "create_channel"}}
```

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "create_channel_binding"}}
```

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "set_reply_binding"}}
```

## Roles and gates

Runner: authenticated user with the verified provider use, channel read access and target workspace access. New channels/triggers are owned by their creator. Trigger bindings need full on the trigger; a reader cannot add/remove a binding or fire it. Workspace reply bindings need full on the workspace and a readable channel whose provider is usable.

| write | shipped gate | authority and decision |
|---|---|---|
| [`system__create_channel_provider`](reference/tools/system/create_channel_provider) | none | admin precondition; supply real secrets privately, never the served mask |
| [`system__create_channel`](reference/tools/system/create_channel) | none | user with provider use; matching platform and unique provider/room pair |
| [`trigger__create`](reference/tools/trigger/create) | required | stamped live approver/admin; inspect source provider/room; approval preserves caller access |
| [`system__create_channel_binding`](reference/tools/system/create_channel_binding) | none | full on trigger plus readable source channel/provider and accessible target; creates a Subscription |
| [`system__set_reply_binding`](reference/tools/system/set_reply_binding) | none | full workspace plus readable channel/provider; stores the standing destination |
| [`system__delete_channel_binding`](reference/tools/system/delete_channel_binding) | none | full on trigger; removes only the named subscription |
| [`system__clear_reply_binding`](reference/tools/system/clear_reply_binding) | none | full workspace; removes its standing destination |
| [`trigger__delete`](reference/tools/trigger/delete) | required | owner/admin; deliberate rollback only; inspect the exact trigger id |

A required worker call parks until the person decides; do not retry while parked or switch to a twin tool. Rejection means no requested effect. Native MCP can report only tool_unavailable for role/exposure/gate refusals. Approval grants this call, not the approver's role or access.

## Steps

1. Create the confirmed room with [`system__create_channel`](reference/tools/system/create_channel). Set chats.enabled=true to permit fresh sessions. The returned channel's owner must be you. This stores the room; it does not itself prove a live listener.

```json
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "team-room", "provider_id": "team-slack", "provider": "slack", "external_id": "C042TEAM", "label": "Team support", "config": {"chats": {"enabled": true}}}}}
```

2. Create a channel-kind source with [`trigger__create`](reference/tools/trigger/create). This exact call parks for required approval; do not retry. The handler still requires read on the channel and use of its provider after approval. Reread its returned id using [`trigger__get`](reference/tools/trigger/get) and retain that id; a trigger id is not its slug.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "team-support", "name": "Team support", "config": {"kind": "channel", "provider_id": "team-slack", "channel_id": "team-room", "interactive": true}}}
```

```json
{"toolset_id": "trigger", "tool_name": "get", "arguments": {"id": "trigger-42"}}
```

3. Add the inbound matcher/action subscription with [`system__create_channel_binding`](reference/tools/system/create_channel_binding). It requires an existing channel-kind trigger; the subscription write itself is ungated by default. The worked matcher accepts human messages starting with help:. The payload template passes the actual event text to the responder. The saving person needs full access on the target workspace (or the target session's workspace), checked again when the subscription fires; a hidden or inaccessible target is refused.

```json
{"toolset_id": "system", "tool_name": "create_channel_binding", "arguments": {"trigger_id": "trigger-42", "event_matcher": {"event_type": "message.posted", "text_pattern": "^help:"}, "config": {"kind": "agent_fresh_session", "workspace_id": "team-work", "agent_id": "team-helper"}, "payload_template": "{{ event.text }}", "reply_target": "source_thread"}}
```

4. Read back the subscription with [`system__list_channel_bindings`](reference/tools/system/list_channel_bindings). Check trigger id, action target, matcher, enabled flag, saved caller and reply_target. Configure the standing workspace destination separately with [`system__set_reply_binding`](reference/tools/system/set_reply_binding), then reread the workspace. A session-scoped source_thread target takes precedence for incoming thread messages; the standing destination covers workspace sessions without that ephemeral target.

```json
{"toolset_id": "system", "tool_name": "list_channel_bindings", "arguments": {"trigger_id": "trigger-42"}}
```

```json
{"toolset_id": "system", "tool_name": "set_reply_binding", "arguments": {"workspace_id": "team-work", "channel_id": "team-room"}}
```

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace", "arguments": {"id": "team-work"}}
```

5. Human activation step: save the channel through the existing Channels console (the owner-authorized REST update), or have an operator restart the API process that owns inbound. Tool writes do not refresh or warm that process's cached channel adapter. A channel row with chats.enabled=true is not proof that its gateway is listening. This [human activation step](not-agent-callable) is required before testing a newly tool-created room; no new warm tool is implied.

6. Use [`trigger__fire_now`](reference/tools/trigger/fire_now) only as a dispatch diagnostic. It supplies no ChannelEvent. For this event-matched binding, expect results[].skipped=true with error_code=skipped_no_match and no session. HTTP 200 or a fire_id is not inbound acceptance; do not remove the matcher to manufacture success.

```json
{"toolset_id": "trigger", "tool_name": "fire_now", "arguments": {"id": "trigger-42"}}
```

7. Send a real human message such as help: show the Cedar on-call guide in the authorized platform room/thread. Inspect the new session in the target workspace and its actual transcript, and verify that the answer arrives in that channel/thread. Nonmatching messages should start no session; bots are not human inbound messages. A stored binding or precondition stop alone is not completion.

## Verification

Reread the room with [`system__get_channel`](reference/tools/system/get_channel), source with [`trigger__get`](reference/tools/trigger/get), binding with [`system__list_channel_bindings`](reference/tools/system/list_channel_bindings) and exact live policies with [`system__describe_tool`](reference/tools/system/describe_tool). Confirm the same caller-owned ids and ready provider permissions.

```json
{"toolset_id": "system", "tool_name": "get_channel", "arguments": {"id": "team-room"}}
```

List actual sessions with [`workspaces__list_workspace_sessions`](reference/tools/workspaces/list_workspace_sessions) and read the returned session id with [`workspaces__get_workspace_session`](reference/tools/workspaces/get_workspace_session).

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "team-work", "limit": 20}}
```

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "team-work", "session_id": "session-42"}}
```

Use only the session id actually returned by the list. Require the session's initiator to be the saved binding owner, the actual message text in its input, a completed turn and an input-derived answer in the platform's correct room/thread. For a manual session with no source-thread override, also verify the standing workspace destination. Keep the default workspace and its access unchanged.

For intentional rollback of this owned test binding only, [`system__delete_channel_binding`](reference/tools/system/delete_channel_binding) removes the saved subscription; reread the list to prove it is absent. [`system__clear_reply_binding`](reference/tools/system/clear_reply_binding) removes the standing workspace destination. Do not clear an unrelated workspace or delete a shared provider. If retiring the owned trigger as well, reread its live delete gate and request [`trigger__delete`](reference/tools/trigger/delete); it parks, do not retry. [`trigger__list`](reference/tools/trigger/list) must then omit the deleted id.

```json
{"toolset_id": "system", "tool_name": "delete_channel_binding", "arguments": {"trigger_id": "trigger-42", "subscription_id": "subscription-42"}}
```

```json
{"toolset_id": "system", "tool_name": "clear_reply_binding", "arguments": {"workspace_id": "team-work"}}
```

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "trigger", "tool_name": "delete"}}
```

```json
{"toolset_id": "trigger", "tool_name": "delete", "arguments": {"id": "trigger-42"}}
```

```json
{"toolset_id": "trigger", "tool_name": "list", "arguments": {"kind": "channel"}}
```

## Refusal and failure

| result | meaning and next move |
|---|---|
| approval_required / parked call | person decides the trigger write; no retry while parked |
| native MCP tool_unavailable | opaque role/exposure/gate refusal; inspect the exact live tool and ask the authorized operator |
| rejected approval | no permission for this requested write; verify unchanged durable state |
| forbidden | provider CRUD needs admin, or a binding/fire needs full; approval never raises authority |
| not-found / trigger_not_found | absent/hidden channel, provider use, trigger or target; ask its owner rather than probing |
| validation-error | platform/config mismatch, invalid token shape/mask, non-channel trigger, invalid matcher/reply target; use the current schema |
| conflict | provider/room pair or trigger slug already exists; inspect the visible inventory before choosing a new id |
| skipped_no_match | no matching event, including manual fire without a ChannelEvent; prove a real inbound message |
| no fresh session | check chats.enabled, enabled trigger/subscription, matcher, listener activation and saved owner's current access |
| session failed or no reply | inspect actual session error, provider connectivity and reply precedence; a create response is not delivery |

## Related

- [channels](channels): credentials, rooms and the two routing surfaces.
- [channels/bindings](channels/bindings): normalized platform events.
- [triggers/triggers](triggers/triggers): channel source and subscription actions.
- [runbooks/create-an-agent](runbooks/create-an-agent): prepare the usable responder.
- [runbooks/manage-a-workspace](runbooks/manage-a-workspace): prepare a separate workspace.
