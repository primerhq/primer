---
title: "Slack question answerer"
summary: "Mentioning a bot in Slack runs an agent that answers from a knowledge collection."
audience: admin
order: 9
tools: [system__create_channel_provider, system__create_channel, workspaces__create_workspace, system__create_agent, trigger__create, system__create_channel_binding, workspaces__list_workspace_sessions, workspaces__get_workspace_session]
related: [channels/bindings, channels/providers]
---
## Goal

Mention the bot in a Slack channel and get an answer from a `company-docs` knowledge collection. A `channel` trigger and an inbound binding on it route each mention to an agent session, and the session answers in the thread the mention came from.

## Prerequisites

- A Slack app with both tokens: the App token (starts with `xapp-`, the Socket Mode token) and the Bot token (starts with `xoxb-`).
- A knowledge collection the bot answers from, e.g. `company-docs`, already populated.
- A model profile the agent runs on, e.g. `anthropic-1--claude-sonnet-4-6`, and a workspace template.
- An admin: creating the provider needs the admin role. The other tools here need the user role and are not approval-gated.

## Steps

### 1. Create the Slack provider
`system__create_channel_provider` stores the app's tokens. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_channel_provider", "arguments": {"entity": {"id": "slack-ops", "provider": "slack", "config": {"app_token": "xapp-REPLACE", "bot_token": "xoxb-REPLACE"}}}}
```

Response (the stored provider, tokens masked):

```json
{"id": "slack-ops", "provider": "slack", "config": {"app_token": "**********", "bot_token": "**********"}}
```

Thread the `id` into the channel below. A token in the wrong field is a `validation-error`: the App token starts with `xapp-`, the Bot token with `xoxb-`.

### 2. Create the channel
`system__create_channel` names the Slack room the bot works in.

```json
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "ops-help", "provider_id": "slack-ops", "provider": "slack", "external_id": "C0123ABC456", "label": "#ops-help", "config": {"chats": {"enabled": true}}}}}
```

Response:

```json
{"id": "ops-help", "provider_id": "slack-ops", "provider": "slack", "external_id": "C0123ABC456", "label": "#ops-help"}
```

`external_id` is the Slack channel id (the last segment of the channel's Copy link), not the room's name. `chats.enabled: true` lets a message in the room start a session: with `false`, the session-starting binding in step 6 never fires for it. Thread the channel `id` into the trigger below. Ask a person to restart primer, or to save the channel in the console, so the bot starts listening.

### 3. Create the workspace
`workspaces__create_workspace` materialises the workspace the sessions run in.

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"template_id": "py-base"}}
```

Response:

```json
{"id": "ws-9f2c", "template_id": "py-base", "phase": "pending"}
```

`phase` is `pending` at the answer and is usable at once. Thread the workspace `id` into the binding below.

### 4. Create the answer agent
`system__create_agent` stores the agent the binding starts.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "answer-bot", "description": "Answers questions from the company-docs collection", "model": {"profile_id": "anthropic-1--claude-sonnet-4-6"}, "tools": ["system__find_documents", "system__get_document"], "system_prompt": ["Answer questions from company-docs. If the answer is not in the collection, say so plainly."]}}}
```

Response:

```json
{"id": "answer-bot", "description": "Answers questions from the company-docs collection"}
```

Scope the agent's `tools` to the collection tools it needs so it retrieves from `company-docs`. Thread the agent `id` into the binding below.

### 5. Create the channel trigger
`trigger__create` stores the `channel` trigger that anchors inbound events for the room.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "ops-help-anchor", "name": "Slack #ops-help", "config": {"kind": "channel", "provider_id": "slack-ops", "channel_id": "ops-help"}}}
```

Response (the created trigger):

```json
{"id": "tr-3f2a9c1d4b7e", "slug": "ops-help-anchor", "name": "Slack #ops-help", "enabled": true}
```

Thread the trigger `id` (the surrogate id, not the slug) into the binding below. Leave `config.channel_id` set so only this room's events fire it.

### 6. Create the binding
`system__create_channel_binding` maps the matcher to the action and the reply target.

```json
{"toolset_id": "system", "tool_name": "create_channel_binding", "arguments": {"trigger_id": "tr-3f2a9c1d4b7e", "event_matcher": {"event_type": "message.posted", "mentions_bot": true}, "config": {"kind": "agent_fresh_session", "workspace_id": "ws-9f2c", "agent_id": "answer-bot"}, "reply_target": "source_thread", "payload_template": "{{ event.text }}"}}
```

Response (the created subscription):

```json
{"id": "sub-a1b2c3", "trigger_id": "tr-3f2a9c1d4b7e", "enabled": true}
```

A message that mentions the bot starts an `answer-bot` session in `ws-9f2c` with the message text. `reply_target: "source_thread"` posts the session's questions and answers in the thread the message was posted in, else the room. A fresh-session binding never busy-skips, so a mention in another thread starts its own session even while an earlier one still runs.

### 7. Test the bot
Post `@answer-bot what is the SLA?` in `#ops-help`. There is no start message: the first thing the bot posts is the answer (or its first question). Find the session:

```json
{"toolset_id": "workspaces", "tool_name": "list_workspace_sessions", "arguments": {"workspace_id": "ws-9f2c"}}
```

Response:

```json
{"items": [{"session_id": "sess-a1b2", "agent_id": "answer-bot", "status": "running"}], "offset": 0, "length": 1, "total": 1}
```

Sessions a binding starts are autonomous, so the session ends when its turn finishes cleanly. Read the state:

```json
{"toolset_id": "workspaces", "tool_name": "get_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2"}}
```

Response:

```json
{"info": {"session_id": "sess-a1b2", "status": "ended", "ended_reason": "completed"}, "status": "ended"}
```

## Verify

A session appears in `ws-9f2c` within a few seconds of the mention, it ends with `ended_reason: "completed"`, and the reply in the Slack thread reflects content from `company-docs`. A second mention in the same thread reopens the same session, so the agent sees the first question too.

## Gotchas

- The bot answers from whatever is in `company-docs` at query time. Stale documents produce stale answers. Re-ingest the collection after each documentation push.
- The matcher fires only for messages that mention the bot (`mentions_bot: true`). The mention stays in `event.text` (the Slack mention markup), so the agent sees its own handle in the message. Drop `mentions_bot` to answer every message in the room.
- `source_thread` maps the session to the thread it started in (an owned anchor): later messages there feed that session, and its posts land there. A workspace-level [reply binding](channels/reply-binding), or an explicit `{channel_id, anchor}` target, is a shared anchor: every session's posts go into the same thread, no thread-to-session mapping is written, and a plain reply typed there starts no session and steers none.
- A message that does not mention the bot starts nothing. A room post (not a thread) starts a session that answers in the room, because `source_thread` falls back to the message's room. Use `source_room` instead when you want such a session to open its own thread in the room.
- Slack rate-limits app messages at roughly one per second per channel. Long answers stream across several messages.
- The Slack app needs the `chat:write`, `channels:read` and `channels:history` scopes and the `message.channels` event, and Socket Mode enabled. No tool here tests a token: a wrong one shows only when the bot connects.

## Related

- [Inbound bindings](channels/bindings)
- [Channel providers](channels/providers)
- [Telegram personal assistant](cookbook/telegram-personal-assistant)
- [Discord moderation helper](cookbook/discord-moderation-helper)
