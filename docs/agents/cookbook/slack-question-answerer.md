---
slug: cookbook/slack-question-answerer
title: Slack Question Answerer
summary: Stand up a Slack channel provider, channel, agent, and association over MCP so that mentioning a bot in Slack runs an agent that answers from an indexed knowledge collection.
mcp_tools:
  - system::create_channel_provider
  - system::create_channel
  - system::create_agent
  - system::create_workspace_channel_association
  - workspaces::create_workspace
  - workspaces::list_workspace_sessions
  - workspaces::get_workspace_session
---

## Goal
Wire a Slack channel to an agent so that mentioning the bot with a question runs a session that answers from a `company-docs` knowledge collection. The channel association routes each incoming Slack message to the agent; no separate trigger is needed.

## Prerequisites
- A Slack app with both tokens: the App token (`xapp-...`, for Socket Mode) and the Bot token (`xoxb-...`).
- A knowledge collection (e.g. `company-docs`) already populated with the documents the bot should answer from; create one with `system::create_collection` if needed.
- An LLM provider configured (its id goes in the agent's `model.provider_id`).
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
    "external_id": "C0123ABC456",
    "label": "#ops-help"
  }
}
```
Response:
```json
{ "id": "ops-help" }
```
`external_id` is the Slack channel ID (the last path segment of the channel's Copy link). Thread `id` ("ops-help") into the association.

### 3. Create the workspace
`workspaces::create_workspace`
```json
{ "template_id": "py-base" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Wait until `phase` is `running`. Thread `id` ("ws-1") into the agent's association.

### 4. Create the answer agent
`system::create_agent`
```json
{
  "entity": {
    "id": "answer-bot",
    "description": "Answers questions from the company-docs collection",
    "model": { "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6" },
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

### 5. Bind the channel to the workspace
`system::create_workspace_channel_association`
```json
{
  "entity": {
    "id": "wca-ops-help",
    "workspace_id": "ws-1",
    "channel_id": "ops-help",
    "enabled": true,
    "forward_ask_user": true,
    "forward_tool_approval": true
  }
}
```
Response:
```json
{ "id": "wca-ops-help" }
```
The association is what routes an incoming Slack message in `#ops-help` to a session in `ws-1`. `forward_ask_user: true` surfaces the agent's `ask_user` prompts back in Slack.

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
- When a channel is associated with exactly one agent, mention-only is the default. Without the `@` mention the bot stays silent.
- Slack rate-limits app messages at roughly one per second per channel. Long answers stream across multiple messages; the channel adapter handles the split.
- Enabling the Slack adapter and delivering the inbound webhook are operator/console steps, not MCP calls. The MCP tools here create the rows; the running adapter does the delivery.

## Related
- `agents`, `sessions`, `channels`, `knowledge`
- `cookbook/create-and-run-a-session`
- `cookbook/telegram-personal-assistant`
- `cookbook/discord-moderation-helper`
