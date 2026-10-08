---
slug: cookbook/slack-question-answerer
title: Slack Question Answerer
summary: Stand up a Slack channel provider, channel, agent, and an inbound channel binding over MCP so that mentioning a bot in Slack runs an agent that answers from an indexed knowledge collection.
mcp_tools:
  - system::create_channel_provider
  - system::create_channel
  - system::create_agent
  - trigger::create
  - system::create_channel_binding
  - workspaces::create_workspace
  - workspaces::list_workspace_sessions
  - workspaces::get_workspace_session
---

## Goal
Wire a Slack channel to an agent so that mentioning the bot with a question runs a session that answers from a `company-docs` knowledge collection. A `channel` trigger and a binding on it route each Slack message that mentions the bot to the agent.

## Prerequisites
- A Slack app with both tokens: the App token (`xapp-...`, for Socket Mode) and the Bot token (`xoxb-...`).
- A knowledge collection (e.g. `company-docs`) already populated with the documents the bot should answer from; create one with `system::create_collection` if needed.
- A ModelProfile for the model the agent runs on, e.g. `anthropic-1--claude-sonnet-4-6` (see "Model profiles" in `agents`); it names a configured LLM provider.
- A workspace template to materialise the agent's workspace from.

## Steps
### 1. Create the Slack channel provider
`system::create_channel_provider`
```json
{
  "entity": {
    "id": "slack-ops",
    "provider": "slack",
    "config": { "app_token": "xapp-REPLACE", "bot_token": "xoxb-REPLACE" }
  }
}
```
Response:
```json
{ "id": "slack-ops" }
```
Slack requires two distinct tokens. The App token starts with `xapp-` and the Bot token with `xoxb-`; the platform rejects a value in the wrong field. Thread `id` ("slack-ops") into the channel's `provider_id`.

### 2. Create the channel
`system::create_channel`
```json
{
  "entity": {
    "id": "ops-help",
    "provider_id": "slack-ops",
    "provider": "slack",
    "external_id": "C0123ABC456",
    "label": "#ops-help",
    "config": { "chats": { "enabled": true } }
  }
}
```
Response:
```json
{ "id": "ops-help" }
```
`external_id` is the Slack channel ID (the last path segment of the channel's Copy link). `config.chats.enabled` lets messages in this room start sessions. Thread `id` ("ops-help") into the trigger below.

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

### 4. Create the answer agent
`system::create_agent`
```json
{
  "entity": {
    "id": "answer-bot",
    "description": "Answers questions from the company-docs collection",
    "model": { "profile_id": "anthropic-1--claude-sonnet-4-6" },
    "tools": ["system__find_documents", "system__get_document"],
    "system_prompt": ["Answer questions from company-docs. If the answer is not in the collection, say so plainly."]
  }
}
```
Response:
```json
{ "id": "answer-bot" }
```
Scope the agent's `tools` to the collection-search tools from the `system` toolset (ids are `<toolset>__<tool>`) so it can retrieve from `company-docs`. The bot strips the `@answer-bot` handle before the text reaches the agent, so do not rely on the agent seeing its own name.

### 5. Route mentions of the bot to the agent
Create the `channel` trigger that anchors inbound events for the room:

`trigger::create`
```json
{
  "slug": "ops-help-anchor",
  "name": "Slack #ops-help",
  "config": { "kind": "channel", "provider_id": "slack-ops", "channel_id": "ops-help" },
  "enabled": true
}
```
Response (the created trigger):
```json
{ "id": "tr-3f2a9c1d4b7e", "slug": "ops-help-anchor", "name": "Slack #ops-help", "enabled": true }
```
Thread `id` ("tr-3f2a9c1d4b7e", the surrogate id, not the slug) into the binding.

Then the binding that maps a matcher to an action:

`system::create_channel_binding`
```json
{
  "trigger_id": "tr-3f2a9c1d4b7e",
  "event_matcher": { "event_type": "message.posted", "mentions_bot": true },
  "config": { "kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "answer-bot" },
  "reply_target": "source_thread",
  "payload_template": "{{ event.text }}"
}
```
Response: the created Subscription. A message that mentions the bot starts an `answer-bot` session in `ws-1`; `reply_target: "source_thread"` posts the answer in the originating thread and also makes the session's gates (`ask_user`, tool approval, `inform`) forward to it. To forward every session of the workspace to the channel regardless of how it started, also bind the workspace with `system::set_reply_binding`.

### 6. Test the bot
Post `@answer-bot what is the SLA?` in the `#ops-help` Slack channel. The channel adapter delivers the message and starts a session. List the workspace's sessions to find it:

`workspaces::list_workspace_sessions`
```json
{ "workspace_id": "ws-1" }
```
Response:
```json
{ "items": [ { "id": "ses-1", "status": "running" } ] }
```
Then poll the session:

`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```

## Verify
A session appears in `ws-1` within a few seconds of the mention and ends with `ended_reason: "completed"`, and the bot's reply in Slack reflects content from `company-docs`.

## Gotchas
- The bot answers from whatever is in `company-docs` at query time. Stale docs produce stale answers; re-ingest after each documentation push.
- The binding fires only for messages that mention the bot (`mentions_bot: true` in the matcher). Without the `@` mention the bot stays silent; drop that field to answer every message in the room.
- Slack rate-limits app messages at roughly one per second per channel. Long answers stream across multiple messages; the channel adapter handles the split.
- Enabling the Slack adapter and delivering the inbound webhook are operator/console steps, not MCP calls. The MCP tools here create the rows; the running adapter does the delivery.

## Related
- `agents`, `sessions`, `channels`, `knowledge`
- `cookbook/create-and-run-a-session`
- `cookbook/telegram-personal-assistant`
- `cookbook/discord-moderation-helper`
