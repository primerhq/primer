---
title: "Discord moderation helper"
summary: "Route Discord messages to a classifier agent and gate its delete tool behind a person's approval."
audience: admin
order: 11
tools: [system__create_channel_provider, system__create_channel, workspaces__create_workspace, system__create_agent, trigger__create, system__create_channel_binding, system__create_tool_approval_policy, workspaces__list_workspace_sessions, workspaces__get_workspace_session]
related: [channels/bindings, approvals/policies]
---
## Goal

Every new message in a Discord channel runs through a classifier agent. The agent's `delete_message` call is gated by a required approval policy, so borderline deletions park the session until a person decides. Clear content passes silently.

## Prerequisites

- A Discord bot token from the Developer Portal, without the `Bot ` prefix (at least 30 characters; the adapter adds it).
- A `discord-tools` toolset registered with a `delete_message` tool, and the MESSAGE CONTENT intent enabled on the application.
- A model profile the agent runs on, e.g. `anthropic-1--claude-sonnet-4-6`, and a workspace template.
- An admin: creating the provider and the approval policy need the admin role. The other tools here need the user role and are not approval-gated.

## Steps

### 1. Create the Discord provider
`system__create_channel_provider` stores the bot's token. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_channel_provider", "arguments": {"entity": {"id": "discord-mod", "provider": "discord", "config": {"bot_token": "REPLACE-WITH-THE-DISCORD-BOT-TOKEN", "enable_dms": false}}}}
```

Response (the stored provider, token masked):

```json
{"id": "discord-mod", "provider": "discord", "config": {"bot_token": "**********", "enable_dms": false}}
```

Thread the `id` into the channel below. `enable_dms: false` keeps a guild-only deployment out of direct messages.

### 2. Create the channel
`system__create_channel` names the text channel to moderate.

```json
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "general-mod", "provider_id": "discord-mod", "provider": "discord", "external_id": "112233445566778899", "label": "#general", "config": {"chats": {"enabled": true}}}}}
```

Response:

```json
{"id": "general-mod", "provider_id": "discord-mod", "provider": "discord", "external_id": "112233445566778899", "label": "#general"}
```

`external_id` is the channel's snowflake (the long number), not the channel's name. `chats.enabled: true` lets a message in the room start a session. Thread the channel `id` into the trigger below. Ask a person to restart primer, or to save the channel in the console, so the bot starts listening.

### 3. Create the workspace
`workspaces__create_workspace` materialises the workspace the sessions run in.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"template_id": "py-base"}}
```

Response:

```json
{"id": "ws-9f2c", "template_id": "py-base", "phase": "pending"}
```

Thread the workspace `id` into the binding below.

### 4. Create the moderator agent
`system__create_agent` stores the classifier.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "moderator", "description": "Classifies Discord messages and deletes clear violations", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": ["discord-tools__delete_message"], "system_prompt": ["Classify each incoming message. Skip messages whose author is yourself to avoid a loop. Call delete_message only for clear violations; borderline cases are gated for a person's approval."]}}}
```

Response:

```json
{"id": "moderator", "description": "Classifies Discord messages and deletes clear violations"}
```

Tell the agent to skip its own messages in the prompt: a matcher that also matches the bot's output creates a moderation loop.

### 5. Create the channel trigger
`trigger__create` stores the `channel` trigger that anchors inbound events for the room.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "general-mod-anchor", "name": "Discord #general", "config": {"kind": "channel", "provider_id": "discord-mod", "channel_id": "general-mod"}}}
```

Response (the created trigger):

```json
{"id": "tr-3f2a9c1d4b7e", "slug": "general-mod-anchor", "name": "Discord #general", "enabled": true}
```

Thread the trigger `id` (the surrogate id, not the slug) into the binding below.

### 6. Create the binding
`system__create_channel_binding` routes every message in the channel to the classifier.

```json
{"toolset_id": "system", "tool_name": "create_channel_binding", "arguments": {"trigger_id": "tr-3f2a9c1d4b7e", "event_matcher": {"event_type": "message.posted", "surface": ["channel"]}, "config": {"kind": "agent_fresh_session", "workspace_id": "ws-9f2c", "agent_id": "moderator"}, "reply_target": "source_thread", "payload_template": "{{ event.text }}"}}
```

Response (the created subscription):

```json
{"id": "sub-a1b2c3", "trigger_id": "tr-3f2a9c1d4b7e", "enabled": true}
```

A message in the room starts a `moderator` session in `ws-9f2c`; a reply in a thread that already has a session steers into it. `reply_target: "source_thread"` posts the session's questions, answers and approval requests in the thread the message was posted in, else the room, so the approval prompt for a parked `delete_message` call lands where the message came from. A fresh-session binding never busy-skips, so sessions for different threads run side by side.

### 7. Gate delete_message behind a person
`system__create_tool_approval_policy` parks every `delete_message` call for a manual decision. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_tool_approval_policy", "arguments": {"entity": {"id": "require-delete-message", "toolset_id": "discord-tools", "tool_name": "delete_message", "approval": {"type": "required"}, "timeout_seconds": 600}}}
```

Response (the stored policy):

```json
{"id": "require-delete-message", "toolset_id": "discord-tools", "tool_name": "delete_message", "approval": {"type": "required"}}
```

A call parked past `timeout_seconds` auto-rejects instead of holding a worker slot. Because the policy names no approvers, the decision may come from the console Inbox or from the Discord approval buttons in the session's thread; a policy that names approvers can be decided in the console only.

### 8. Observe a parked deletion
When a borderline message arrives and the agent calls `delete_message`, the session parks. Find it:

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "ws-9f2c"}}
```

Response:

```json
{"items": [{"session_id": "sess-a1b2", "agent_id": "moderator", "status": "running"}], "offset": 0, "length": 1, "total": 1}
```

Read its lifecycle:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2"}}
```

Response:

```json
{"info": {"session_id": "sess-a1b2", "status": "running"}, "status": "running"}
```

The session reads `status: "running"` while parked. The full row says why: its `parked_event_key` starts with `tool_approval:` (read it with `workspaces__find_sessions`, [find sessions](sessions/find-sessions)). Approving resumes the session and deletes the message; rejecting returns a clean error the agent reports.

## Verify

A borderline message parks the session on the `delete_message` call (`status: "running"`, `parked_event_key` starting with `tool_approval:`), no deletion happens until a person approves, and a clear violation is deleted without a park once the agent is confident.

## Gotchas

- The bot's own posts pass through the binding too. The prompt instruction to skip self-authored messages is what keeps the loop from forming.
- Discord fills `sender.roles` (the sender's guild role IDs, not names, without `@everyone`); Slack and Telegram do not. Use `sender_roles_any` in the matcher to run the moderator only for messages from members with a given role. To copy a role id, enable Developer Mode (User Settings > Advanced), then right-click the role and choose Copy Role ID.
- Discord slash commands are not received (neither are Slack's): `command.invoked` comes from Telegram only, so a slash command in the room starts nothing.
- The steering rule depends on the anchor kind. This recipe's `source_thread` is an owned anchor: the session's own thread, and a reply typed there feeds that session. A shared anchor (a workspace [reply binding](channels/reply-binding), or an explicit `{channel_id, anchor}` target) posts every session's traffic into the same thread and writes no thread-to-session mapping, so a plain reply typed there starts no session and steers none.
- Discord webhook delivery is delayed by a second or two. Calibrate `timeout_seconds` against the classifier's expected response window.
- Deleting the policy does not resolve already-parked sessions. Decide those first. Once the classifier is well calibrated, switch `approval.type` to `llm` and let a judge decide high-confidence deletions.

## Related

- [Inbound bindings](channels/bindings)
- [Approval policies](approvals/policies)
- [Approval-gated deploy bot](cookbook/approval-gated-deploy-bot)
- [Slack question answerer](cookbook/slack-question-answerer)
