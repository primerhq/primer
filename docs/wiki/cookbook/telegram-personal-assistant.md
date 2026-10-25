---
title: "Telegram personal assistant"
summary: "Direct messages to a Telegram bot continue one durable session across messages."
audience: admin
order: 10
tools: [system__create_channel_provider, system__create_channel, workspaces__create_workspace, system__create_agent, trigger__create, system__create_channel_binding, workspaces__list_workspace_sessions, workspaces__get_workspace_session]
related: [channels/bindings, channels/providers]
---
## Goal

A Telegram bot you DM to get a personal assistant. Each DM lands in one durable session: the first message starts it, every later message in the same conversation continues it, so the agent keeps the full transcript across messages.

## Prerequisites

- A Telegram bot token from BotFather, in the `<digits>:<secret>` shape (at least 20 characters).
- Your numeric Telegram user id (send any message to `@userinfobot` to get it). It is the channel's `external_id`.
- A model profile the agent runs on, e.g. `anthropic-1--claude-sonnet-4-6`, and a workspace template.
- An admin: creating the provider needs the admin role. The other tools here need the user role and are not approval-gated.

## Steps

### 1. Create the Telegram provider
`system__create_channel_provider` stores the bot's token. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_channel_provider", "arguments": {"entity": {"id": "tg-personal", "provider": "telegram", "config": {"bot_token": "1234567890:ABCDefGhIJKlmNoPQRstUVWxyz", "poll_timeout_seconds": 25}}}}
```

Response (the stored provider, token masked):

```json
{"id": "tg-personal", "provider": "telegram", "config": {"bot_token": "**********", "poll_timeout_seconds": 25}}
```

Thread the `id` into the channel below. `poll_timeout_seconds` is the long-poll window per getUpdates call (1 to 60, default 25): lower it for faster delivery at the cost of more API calls.

### 2. Add your private conversation as a channel
`system__create_channel` names the DM room the bot works in.

```json
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "personal-dm", "provider_id": "tg-personal", "provider": "telegram", "external_id": "987654321", "label": "personal DM", "config": {"chats": {"enabled": true}}}}}
```

Response:

```json
{"id": "personal-dm", "provider_id": "tg-personal", "provider": "telegram", "external_id": "987654321", "label": "personal DM"}
```

`external_id` is your numeric user id, not a group id: binding a single private user keeps the bot from acting on group messages. `chats.enabled: true` lets a message in the room start a session. Thread the channel `id` into the trigger below. Ask a person to restart primer, or to save the channel in the console, so the bot starts listening.

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

### 4. Create the assistant agent
`system__create_agent` stores the agent the binding starts.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "personal-assistant", "description": "Concise personal assistant that tracks to-do items", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": ["web__web_search"], "system_prompt": ["You are a personal assistant. Be concise. Track to-do items the user mentions in the conversation."]}}}
```

Response:

```json
{"id": "personal-assistant", "description": "Concise personal assistant that tracks to-do items"}
```

Scope the agent's `tools` to what the assistant needs. The session carries its history, so the agent sees prior turns without being told them again.

### 5. Create the channel trigger
`trigger__create` stores the `channel` trigger that anchors inbound events for the DM.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "personal-dm-anchor", "name": "Telegram personal DM", "config": {"kind": "channel", "provider_id": "tg-personal", "channel_id": "personal-dm"}}}
```

Response (the created trigger):

```json
{"id": "tr-3f2a9c1d4b7e", "slug": "personal-dm-anchor", "name": "Telegram personal DM", "enabled": true}
```

Thread the trigger `id` (the surrogate id, not the slug) into the binding below.

### 6. Create the binding
`system__create_channel_binding` routes DMs to the assistant.

```json
{"toolset_id": "system", "tool_name": "create_channel_binding", "arguments": {"trigger_id": "tr-3f2a9c1d4b7e", "event_matcher": {"event_type": "message.posted", "surface": ["dm"]}, "config": {"kind": "agent_fresh_session", "workspace_id": "ws-9f2c", "agent_id": "personal-assistant"}, "reply_target": "source_thread", "payload_template": "{{ event.text }}"}}
```

Response (the created subscription):

```json
{"id": "sub-a1b2c3", "trigger_id": "tr-3f2a9c1d4b7e", "enabled": true}
```

A DM starts a `personal-assistant` session in `ws-9f2c`, and later messages in the same conversation continue that session: a DM conversation maps to one session, and the session's questions and answers come back into it (`reply_target: "source_thread"` answers in the conversation the message came from). There is no start message: the first thing the bot posts is the answer.

### 7. Test the assistant
Send a DM to the bot. Find the session:

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "ws-9f2c"}}
```

Response:

```json
{"items": [{"session_id": "sess-a1b2", "agent_id": "personal-assistant", "status": "running"}], "offset": 0, "length": 1, "total": 1}
```

Then read the state:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2"}}
```

Response:

```json
{"info": {"session_id": "sess-a1b2", "status": "ended", "ended_reason": "completed"}, "status": "ended"}
```

Send a second DM. It lands in the same session, and the agent sees both messages.

## Verify

The bot replies within a few seconds. One session appears in `ws-9f2c`, it ends with `ended_reason: "completed"`, and a second DM continues the same session rather than starting a fresh one.

## Gotchas

- Bind the personal user id, not a group id: a Telegram bot in a group receives every message in it, and the binding would start sessions for the whole group.
- A message that starts with `/name` (or `/name@bot`) is a command, not a message: it arrives as a `command.invoked` event with `command.name` and `command.args`, and it is never a mention. To make the assistant handle slash commands, add a second binding on the same trigger with `event_type: "command.invoked"` and `command_name` in its matcher. `command.invoked` comes from Telegram only; Slack and Discord slash commands are not received at all.
- A Telegram bot reads group messages only with privacy mode off (BotFather) or as a group admin. A DM needs no such setting.
- Telegram has no threads: `reply_target` anchors and workspace reply-binding anchors are ignored there, and posts go straight into the conversation.
- No tool here tests the token. A wrong one shows only when the bot connects: the room stays silent and the error is in the server log.

## Related

- [Inbound bindings](channels/bindings)
- [Channel providers](channels/providers)
- [Slack question answerer](cookbook/slack-question-answerer)
- [Create and run a session](cookbook/create-and-run-a-session)
