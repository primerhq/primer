---
title: "Channels"
summary: "A channel is one room on a provider. Create and change channels, and let a room start sessions."
order: 2
tools: [system__list_channels, system__get_channel, system__create_channel, system__update_channel, system__delete_channel, system__find_channels]
related: [channels/providers, channels/bindings, channels/reply-binding]
---
## What it is
A channel is one room on a [channel provider](channels/providers): a Slack channel, a Telegram group or DM, or a Discord channel, named by the platform's own id. The bot hears and posts only in rooms that have a channel. Manage them with `system__list_channels`, `system__get_channel`, `system__find_channels`, `system__create_channel`, `system__update_channel` and `system__delete_channel`. These tools need the `user` role. The provider a channel names is an admin's to create.

## Concepts
- **Fields.** `id` (optional on create: the server assigns `channel-<hex>`), `provider_id` (the provider row), `provider` (`slack`, `telegram` or `discord`: it must be that provider's platform), `external_id` (the platform's room id), `label` (optional, at most 200 characters) and `config`.
- **`external_id` is the platform's id, not the room's name.** A Slack channel id looks like `C012ABCDEF`. A Telegram room id is a number (a group's is negative). A Discord channel id is a long number. Put the name in `label`.
- **One room, one channel.** The pair `provider_id` and `external_id` belongs to one channel only.
- **`config.chats`** has two keys and no others: `enabled` (default `false`) and `relay_mode` (`final` or `all`, default `final`).
- **`chats.enabled: true` lets the room start sessions.** The bot listens in a room only while the room's listener runs. A room with `chats.enabled` gets its listener when primer starts, and when a person saves the channel in the console. Any room also gets one the first time a session posts to it.
- **The channel does not pick the agent.** A message starts the agent or graph that a [binding](channels/bindings) on a `channel` trigger names, so one room can route to different agents by rule.
- **`relay_mode` is stored, but the server does not read it today.** How much a conversation hears back is set by the `interactive` flag of the channel trigger ([inbound bindings](channels/bindings)).
- **A DM is a room too.** A direct message with the bot needs its own channel, with the DM's id as `external_id` (for Telegram, the person's user id).
- **One channel serves many workspaces.** Inbound bindings and the [reply bindings](channels/reply-binding) of any number of workspaces can share it.
- **The running bot.** The listeners run in the API process, and a tool may run in another one. So a new channel, a changed one, or `chats.enabled` turned on is heard after a restart, or after a person saves the channel in the console.
- None of these tools is approval-gated by default.

## How to
1. Get the provider's `id` from an admin, or read it off an existing channel with `system__list_channels`.
2. Get the room's id from the platform.
3. Check the room has no channel yet: `system__find_channels` on `external_id`.
4. Create the channel with `system__create_channel`. Set `chats.enabled: true` when messages there should start sessions.
5. Ask a person to restart primer, or to save the channel in the console, so the bot starts listening.
6. Route messages with a [binding](channels/bindings), or send a workspace's questions here with a [reply binding](channels/reply-binding).

## Tool calls
### Create a channel that starts sessions
`system__create_channel` stores the room. Here a Slack channel whose messages may start sessions.

```json
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "ch-slack-oncall", "provider_id": "cp-slack", "provider": "slack", "external_id": "C012ABCDEF", "label": "#oncall", "config": {"chats": {"enabled": true, "relay_mode": "final"}}}}}
```

Reference: [`system__create_channel`](reference/tools/system/create_channel)

### Create a Telegram DM channel
A direct message room with a Telegram bot, keyed by the person's user id.

```json
{"toolset_id": "system", "tool_name": "create_channel", "arguments": {"entity": {"id": "ch-tg-helpdesk", "provider_id": "cp-telegram", "provider": "telegram", "external_id": "987654321", "label": "helpdesk DM", "config": {"chats": {"enabled": true}}}}}
```

### Read a channel
`system__get_channel` returns the whole row.

```json
{"toolset_id": "system", "tool_name": "get_channel", "arguments": {"id": "ch-slack-oncall"}}
```

Reference: [`system__get_channel`](reference/tools/system/get_channel)

### List channels
`system__list_channels` pages through every channel.

```json
{"toolset_id": "system", "tool_name": "list_channels", "arguments": {"limit": 50}}
```

Reference: [`system__list_channels`](reference/tools/system/list_channels)

### Find the channel of a room
`system__find_channels` filters by a field, here the platform's room id.

```json
{"toolset_id": "system", "tool_name": "find_channels", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "external_id"}, "op": "=", "right": {"kind": "value", "value": "C012ABCDEF"}}}}
```

Reference: [`system__find_channels`](reference/tools/system/find_channels)

### Rename a channel
`system__update_channel` replaces the whole row: read it, change it, send it all back.

```json
{"toolset_id": "system", "tool_name": "update_channel", "arguments": {"id": "ch-slack-oncall", "entity": {"id": "ch-slack-oncall", "provider_id": "cp-slack", "provider": "slack", "external_id": "C012ABCDEF", "label": "#oncall-primary", "config": {"chats": {"enabled": true, "relay_mode": "final"}}}}}
```

Reference: [`system__update_channel`](reference/tools/system/update_channel)

### Delete a channel
`system__delete_channel` removes the row and answers `{deleted, id}`.

```json
{"toolset_id": "system", "tool_name": "delete_channel", "arguments": {"id": "ch-tg-helpdesk"}}
```

Reference: [`system__delete_channel`](reference/tools/system/delete_channel)

## Gotchas
- A room with no channel is never heard, even when a binding names its provider.
- A `provider` that is not the provider's platform is a `validation-error` naming both platforms. So is a `provider_id` that names no provider.
- A pair that another channel holds is a `conflict` naming that channel.
- An unknown key in `config.chats`, such as the retired `default_agent` or `allowed_agents`, is a `validation-error`.
- An update replaces the whole row. Leave out `config` and `chats.enabled` falls back to `false`: after the next restart the bot no longer listens there until a session posts to the room.
- `chats.enabled: false` is not a block: a room that a session posts to is heard again. To stop a room starting sessions, disable its `channel` trigger or delete its binding.
- The tools do not reach the running listener. A deleted channel's room can still be heard until a restart.
- Nothing blocks deleting a channel that a workspace's reply binding or a `channel` trigger still names. The reply binding then posts nothing (a server log line only), and the trigger never fires. Clear the reply binding and fix the trigger first.

## Related
[channel providers](channels/providers), [inbound bindings](channels/bindings), [reply binding](channels/reply-binding).
