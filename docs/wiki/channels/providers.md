---
title: "Channel providers"
summary: "Admin: register the Slack, Telegram or Discord integration that channels belong to."
audience: admin
order: 1
tools: [system__list_channel_providers, system__get_channel_provider, system__create_channel_provider, system__update_channel_provider, system__delete_channel_provider, system__find_channel_providers]
related: [channels/channels, not-agent-callable]
---
## What it is
A channel provider is one messaging integration: one Slack app, one Telegram bot or one Discord bot, with its tokens. Every [channel](channels/channels) belongs to a provider. Every tool on this page needs an admin: `system__list_channel_providers`, `system__get_channel_provider`, `system__find_channel_providers`, `system__create_channel_provider`, `system__update_channel_provider` and `system__delete_channel_provider`.

## Concepts
- **`provider`** is `slack`, `telegram` or `discord`. `config` must match it:
  - `slack`: `app_token` (starts with `xapp-`; the app-level token Socket Mode needs), `bot_token` (starts with `xoxb-`) and an optional `signing_secret`, which nothing uses: the bot connects over Socket Mode, so primer needs no public URL.
  - `telegram`: `bot_token` from BotFather (digits, a colon, then the secret: at least 20 characters) and `poll_timeout_seconds` (1 to 60, default 25: how long each long poll waits).
  - `discord`: `bot_token` from the Developer Portal (at least 30 characters, without the `Bot ` prefix) and `enable_dms` (default `true`: the bot also reads direct messages).
- **`id`** is optional on create. The server assigns `channel-provider-<hex>`. It never changes.
- **Secrets are write-only.** A read shows every token masked. An update that sends a mask back unchanged keeps the stored token. A create that carries a mask is refused: send the real tokens. A mask for a secret that was never stored (a Slack `signing_secret` you never set) is refused too, and the error names the field.
- **Platform settings primer cannot make.** Slack: the scopes `chat:write`, `channels:read` and `channels:history`, and the `message.channels` event. Discord: the MESSAGE CONTENT intent. Telegram: turn privacy mode off in BotFather, or make the bot a group admin, so it reads group messages.
- **One connection per provider.** Every channel of a provider shares the bot's connection.
- **The running bot.** The bot runs in the API process. A tool may run in another process, so these tools do not restart the bot: a new token, or a deleted provider, takes effect after a restart. A person who saves the provider in the console drops the old connections at once.
- None of these tools is approval-gated by default.

## How to
1. Create the app or bot on the platform, set the platform settings above, and get its tokens from a person. Never put a token in a message, a file or a session transcript.
2. Create the provider with `system__create_channel_provider`. Keep its `id`.
3. Add a channel for each room the bot works in ([channels](channels/channels)).
4. To rotate a token, update the provider with the new token, then ask a person to restart primer or save the provider in the console.
5. To remove a provider, delete its channels first, then the provider.

## Tool calls
### Create a Slack provider
`system__create_channel_provider` stores the integration. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_channel_provider", "arguments": {"entity": {"id": "cp-slack", "provider": "slack", "config": {"app_token": "xapp-REPLACE", "bot_token": "xoxb-REPLACE"}}}}
```

Reference: [`system__create_channel_provider`](reference/tools/system/create_channel_provider)

### Create a Telegram provider
A Telegram bot token holds a colon. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_channel_provider", "arguments": {"entity": {"id": "cp-telegram", "provider": "telegram", "config": {"bot_token": "123456:REPLACE-WITH-THE-BOT-TOKEN", "poll_timeout_seconds": 25}}}}
```

### Create a Discord provider
Send the bare token, without `Bot `. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_channel_provider", "arguments": {"entity": {"id": "cp-discord", "provider": "discord", "config": {"bot_token": "REPLACE-WITH-THE-DISCORD-BOT-TOKEN", "enable_dms": false}}}}
```

### Read a provider
`system__get_channel_provider` returns the row with its tokens masked. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "get_channel_provider", "arguments": {"id": "cp-telegram"}}
```

Reference: [`system__get_channel_provider`](reference/tools/system/get_channel_provider)

### List providers
`system__list_channel_providers` pages through every provider. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "list_channel_providers", "arguments": {"limit": 20}}
```

Reference: [`system__list_channel_providers`](reference/tools/system/list_channel_providers)

### Find the providers of one platform
`system__find_channel_providers` filters rows by a field. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "find_channel_providers", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "provider"}, "op": "=", "right": {"kind": "value", "value": "slack"}}}}
```

Reference: [`system__find_channel_providers`](reference/tools/system/find_channel_providers)

### Change a setting and keep the token
`system__update_channel_provider` replaces the whole row. Send back the masked token exactly as the read showed it, and it is kept. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "update_channel_provider", "arguments": {"id": "cp-telegram", "entity": {"id": "cp-telegram", "provider": "telegram", "config": {"bot_token": "**********", "poll_timeout_seconds": 50}}}}
```

Reference: [`system__update_channel_provider`](reference/tools/system/update_channel_provider)

### Delete a provider
`system__delete_channel_provider` removes the row and answers `{deleted, id}`. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "delete_channel_provider", "arguments": {"id": "cp-discord"}}
```

Reference: [`system__delete_channel_provider`](reference/tools/system/delete_channel_provider)

## Gotchas
- Every tool here needs an admin. A run below it gets `forbidden`.
- A token in the wrong field, or in the wrong shape, is a `validation-error`: a Slack bot token in `app_token` fails the `xapp-` check, and a Discord token with the `Bot ` prefix is refused.
- A provider that a channel still uses cannot be deleted (`conflict`). Delete its channels first.
- No tool tests a token. A wrong token shows only when the bot connects: the room stays silent and the error is in the server log. Ask a person to check.
- The tools do not reach the running bot. After a token change or a delete, ask a person to restart primer.
- An update replaces the whole row. Send every config field: one you leave out falls back to its default.
- Never echo a token back to a person or into a file. Ask the person to paste it into the console instead when they can.

## Related
[channels](channels/channels), [inbound bindings](channels/bindings), [reply binding](channels/reply-binding), [not agent-callable](not-agent-callable).
