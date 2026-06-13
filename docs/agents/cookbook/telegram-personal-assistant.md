---
slug: cookbook/telegram-personal-assistant
title: Telegram Personal Assistant
summary: Wire a Telegram bot to a persistent chat agent over MCP so DMing the bot continues one durable conversation across messages.
mcp_tools:
  - system::create_channel_provider
  - system::create_channel
  - system::create_agent
  - system::set_workspace_channel_association
  - workspaces::create_workspace
  - workspaces::list_workspace_sessions
  - workspaces::get_workspace_session
---

## Goal
A Telegram bot you DM to get a personal assistant. The channel association routes each DM to the agent, and the chat thread keyed to your Telegram user ID persists across messages so the agent sees the full transcript every turn.

## Prerequisites
- A Telegram bot token from `@BotFather`, in the `<id>:<hash>` shape (at least 20 characters).
- Your numeric Telegram user ID (send any message to `@userinfobot` to get it); this is the channel's `external_id`.
- An LLM provider configured and a workspace template available.

## Steps
### 1. Create the Telegram channel provider
`system::create_channel_provider`
```json
{
  "entity": {
    "id": "tg-personal",
    "provider": "telegram",
    "config": { "bot_token": "1234567890:ABCDefGhIJKlmNoPQRstUVWxyz", "poll_timeout_seconds": 25 }
  }
}
```
Response:
```json
{ "id": "tg-personal" }
```
`bot_token` must contain a `:` and be at least 20 characters. `poll_timeout_seconds` is the long-poll window per getUpdates; lower it for faster delivery at the cost of more API calls.

### 2. Add your private chat as a channel
`system::create_channel`
```json
{
  "entity": {
    "id": "personal-dm",
    "provider_id": "tg-personal",
    "external_id": "987654321",
    "label": "personal-dm"
  }
}
```
Response:
```json
{ "id": "personal-dm" }
```
`external_id` is your numeric Telegram user ID from `@userinfobot`. Binding to a single private user ID (not a group chat ID) keeps the bot from acting on group messages.

### 3. Create the workspace
`workspaces::create_workspace`
```json
{ "template_id": "py-base" }
```
Response:
```json
{ "id": "ws-1", "phase": "running" }
```
Wait until `phase` is `running`. Thread `id` ("ws-1") into the association.

### 4. Create the personal assistant agent
`system::create_agent`
```json
{
  "entity": {
    "id": "personal-assistant",
    "description": "Concise personal assistant that tracks to-do items",
    "model": { "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6" },
    "tools": ["web__search"],
    "system_prompt": ["You are a personal assistant. Be concise. Track to-do items the user mentions in the conversation."]
  }
}
```
Response:
```json
{ "id": "personal-assistant" }
```
Scope `tools` to the toolsets the assistant needs (here a `web` search tool). The chat thread carries history, so the agent sees prior turns without re-stating them.

### 5. Bind the channel to the workspace
`system::set_workspace_channel_association`
```json
{
  "workspace_id": "ws-1",
  "channel_id": "personal-dm"
}
```
Response:
```json
{ "ok": true, "workspace_id": "ws-1", "channel_id": "personal-dm" }
```
The association routes all session gates (`ask_user`, tool approval, `inform`) from sessions in `ws-1` to the Telegram channel. All gate types forward automatically; `ask_user` prompts from the agent are delivered to Telegram.

### 6. Test the assistant
DM the bot in Telegram. The adapter delivers the message and starts a session for the turn. Find it:

`workspaces::list_workspace_sessions`
```json
{ "workspace_id": "ws-1" }
```
Response:
```json
{ "items": [ { "id": "ses-1", "status": "running" } ] }
```
Poll it to completion:

`workspaces::get_workspace_session`
```json
{ "workspace_id": "ws-1", "session_id": "ses-1" }
```
Response:
```json
{ "id": "ses-1", "status": "ended", "ended_reason": "completed" }
```
Send a second DM. It lands in the same chat thread; the agent sees both messages.

## Verify
The bot replies within a few seconds, a session appears in `ws-1` per turn ending with `ended_reason: "completed"`, and a second DM continues the same chat thread rather than starting a fresh one.

## Gotchas
- Telegram bots receive every message in any group they are added to. Bind to a single private user ID, not a group channel ID, to keep the assistant DM-only.
- Long responses split at the 4096-character mark across multiple Telegram messages; the channel adapter handles the split.
- Creating the BotFather bot, enabling the Telegram adapter, and the inbound DM delivery are Telegram/operator steps, not MCP calls. The MCP tools here create the rows; the running adapter does the delivery.

## Related
- `agents`, `chats`, `sessions`, `channels`
- `cookbook/slack-question-answerer`
- `cookbook/create-and-run-a-session`
