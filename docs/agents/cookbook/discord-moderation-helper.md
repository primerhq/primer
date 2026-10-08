---
slug: cookbook/discord-moderation-helper
title: Discord Moderation Helper
summary: Route every Discord message in a channel to a classifier agent and gate its delete_message tool behind a required approval policy so a human decides borderline deletions.
mcp_tools:
  - system::create_channel_provider
  - system::create_channel
  - system::create_agent
  - trigger::create
  - system::create_channel_binding
  - system::create_tool_approval_policy
  - workspaces::create_workspace
  - workspaces::list_workspace_sessions
  - workspaces::get_workspace_session
---

## Goal
Every new Discord message in a moderated channel runs through a classifier agent. The agent's `delete_message` call is gated by a required approval policy, so borderline deletions park the session and wait for a human decision. Clear content passes silently.

## Prerequisites
- A Discord bot token from the Developer Portal (without the `Bot ` prefix; the adapter adds it). Must be at least 30 characters.
- A `discord-tools` toolset registered with a `delete_message` tool.
- An LLM provider configured and a workspace template available.

## Steps
### 1. Create the Discord channel provider
`system::create_channel_provider`
```json
{
  "entity": {
    "id": "discord-mod",
    "provider": "discord",
    "config": { "bot_token": "REPLACE_WITH_30PLUS_CHAR_TOKEN", "enable_dms": false }
  }
}
```
Response:
```json
{ "id": "discord-mod" }
```
Do not include the `Bot ` prefix in `bot_token`. Set `enable_dms: false` for a guild-only moderation deployment.

### 2. Create the channel
`system::create_channel`
```json
{
  "entity": {
    "id": "general-mod",
    "provider_id": "discord-mod",
    "provider": "discord",
    "external_id": "112233445566778899",
    "label": "#general",
    "config": { "chats": { "enabled": true } }
  }
}
```
Response:
```json
{ "id": "general-mod" }
```
`external_id` is the channel snowflake of the text channel to moderate. `config.chats.enabled` lets messages in this room start sessions.

### 3. Create the workspace
`workspaces::create_workspace`
```json
{ "template_id": "py-base" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Wait until `phase` is `running`. Thread `id` ("ws-1") into the binding below.

### 4. Create the moderator agent
`system::create_agent`
```json
{
  "entity": {
    "id": "moderator",
    "description": "Classifies Discord messages and deletes clear violations",
    "model": { "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6" },
    "tools": ["discord-tools__delete_message"],
    "system_prompt": ["Classify each incoming message. Skip messages whose author is yourself to avoid a loop. Call delete_message only for clear violations; borderline cases will be gated for human review."]
  }
}
```
Response:
```json
{ "id": "moderator" }
```
Instruct the agent in its prompt to skip its own messages; a pattern that matches the bot's own output creates a moderation loop.

### 5. Route every message in the channel to the moderator
Create the `channel` trigger that anchors inbound events for the room:

`trigger::create`
```json
{
  "slug": "general-mod-anchor",
  "name": "Discord #general",
  "config": { "kind": "channel", "provider_id": "discord-mod", "channel_id": "general-mod" },
  "enabled": true
}
```
Response (the created trigger):
```json
{ "id": "tr-3f2a9c1d4b7e", "slug": "general-mod-anchor", "name": "Discord #general", "enabled": true }
```
Thread `id` ("tr-3f2a9c1d4b7e", the surrogate id, not the slug) into the binding.

Then the binding that maps a matcher to an action:

`system::create_channel_binding`
```json
{
  "trigger_id": "tr-3f2a9c1d4b7e",
  "event_matcher": { "event_type": "message.posted", "surface": ["channel"] },
  "config": { "kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "moderator" },
  "reply_target": "source_thread",
  "payload_template": "{{ event.text }}",
  "parallelism": "queue"
}
```
Response: the created Subscription. Every message in the room starts a `moderator` session in `ws-1` (`parallelism: "queue"` gives each message its own session even while an earlier one is still running). `reply_target: "source_thread"` makes the session's gates (`ask_user`, tool approval, `inform`) forward to the Discord thread, so the approval prompt for a parked `delete_message` call is delivered there. To forward every session of the workspace to the channel regardless of how it started, also bind the workspace with `system::set_reply_binding`.

### 6. Create the required approval policy on delete_message
`system::create_tool_approval_policy`
```json
{
  "entity": {
    "id": "require-delete-message",
    "toolset_id": "discord-tools",
    "tool_name": "delete_message",
    "approval": { "type": "required" },
    "timeout_seconds": 600
  }
}
```
Response:
```json
{ "id": "require-delete-message" }
```
`type: "required"` makes every `delete_message` call park the session and wait for a manual decision. The `timeout_seconds` causes calls parked past 600s to auto-reject rather than hold a worker slot indefinitely.

### 7. Observe a parked deletion
When a borderline message arrives and the agent calls `delete_message`, the session parks. List sessions to find it:

`workspaces::list_workspace_sessions`
```json
{ "workspace_id": "ws-1" }
```
Response:
```json
{ "items": [ { "id": "ses-1", "status": "waiting" } ] }
```
Inspect the parked session:

`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "waiting" }
```

## Verify
A borderline message produces a session with `status: "waiting"` parked on the `delete_message` call. There is no MCP tool to approve; an operator resolves it from the console Approvals > Pending tab or via the REST respond endpoint. Approving resumes the session and deletes the message; rejecting returns a clean error to the agent.

## Gotchas
- Approving or rejecting a parked call is an operator/REST action, not an MCP call. The MCP tools here create the policy and observe the parked state.
- Deleting an approval policy does not resolve already-parked sessions. Decide them manually first.
- Discord webhook delivery is slightly delayed (1 to 5 seconds typical). Calibrate `timeout_seconds` against the moderator's expected response window.
- Once the classifier's confidence is well calibrated, switch the policy `approval.type` to `llm` (an LLM judge) to auto-allow high-confidence deletions and cut moderator load.

## Related
- `agents`, `sessions`, `channels`, `tool-approval`
- `cookbook/approval-gated-deploy-bot`
- `cookbook/slack-question-answerer`
