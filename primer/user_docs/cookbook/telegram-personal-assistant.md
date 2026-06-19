---
slug: telegram-personal-assistant
title: Telegram personal assistant
section: cookbook
summary: A persistent chat agent reachable from a private Telegram chat.
difficulty: beginner
time_minutes: 15
tags: [channels, agents, chats]
---

## Goal

A Telegram bot you DM to get a personal assistant. Each new message
continues the same persistent chat thread. Useful for reminders, quick
research, and durable to-do lists that survive across app restarts.

## Prerequisites

- A Telegram account and access to `@BotFather` to create a bot and
  obtain its token.
- Your Telegram user ID (send any message to `@userinfobot` to get it).

## Steps

### 1. Create a Telegram bot via @BotFather

1. Open Telegram and start a chat with `@BotFather`.
2. Send `/newbot` and follow the prompts to name the bot and choose a
   username ending in `bot`.
3. Copy the bot token that BotFather sends; it looks like
   `1234567890:ABCDefGhIJKlmNoPQRstUVWxyz`.

### 2. Add a Telegram channel provider

1. Open **Channels** in the left nav and switch to the **Providers**
   tab.
2. Click **New provider** and select **Telegram**.
3. Paste the bot token into the **Bot token** field. The token must be
   at least 20 characters in the `<id>:<hash>` format.
4. Click **Create provider**.

```embed:channels
```

### 3. Add your private chat as a channel

1. Switch to the **Channels** tab and click **New channel**.
2. Select the Telegram provider you just created.
3. Enter your numeric Telegram user ID in the **External ID** field
   (the one you got from `@userinfobot`).
4. Add a label such as `personal-dm`.
5. Click **Create channel**.

### 4. Create a workspace

The agent needs a workspace to run in.

1. Open **Workspaces** and click **New workspace**.
2. Select a template and click **Create**.

### 5. Link the channel to the workspace

1. Open the workspace and switch to its **Channels** tab.
2. Click **Link channel** and select the `personal-dm` channel.
3. Confirm. All session gates from that workspace, including ask_user,
   now forward to Telegram automatically; there are no per-gate toggles.

### 6. Create the personal assistant agent

1. Open **Agents** and click **New agent**.
2. In the **Basic** tab, set a description such as
   `personal-assistant`, pick your LLM provider and model.
3. In the **Tools** tab, select tools from `system`, `web`, and `misc`
   toolsets as needed.
4. In the **Advanced** tab, enter a system prompt such as: "You are a
   personal assistant. Be concise. Track to-do items the user mentions
   in the conversation."
5. Click **Create**.

```embed:agents-page
```

### 7. Test the assistant

Send a DM to your bot in Telegram. Within a few seconds the bot should
reply. Open **Chats** in the console to see the active chat thread that
was created.

```embed:chat-stream
```

The chat thread persists across messages. Your second message lands in
the same chat as your first, and the agent sees the full transcript each
turn.

```callout:info
The chat thread is keyed to your Telegram user ID. As long as you DM
the same bot, every message continues the same persistent conversation.
```

## Gotchas

```callout:warning
Telegram bots receive every message in any group they are added to, not
just direct messages. If you want the bot to ignore group mentions, bind
it to a single private chat ID rather than a group channel ID.
```

- Telegram caches the bot's profile picture aggressively. Changing the
  avatar may take a few hours to propagate to all clients.
- Long agent responses are split into multiple Telegram messages at the
  4096-character mark. The channel adapter handles the split
  automatically.
- The poll timeout on the Telegram provider defaults to 25 seconds.
  Reduce it if you need faster delivery, but watch for increased API
  call volume.

## Automate this

```ref:reference/api-channels
The API reference covers channel providers, channels, and workspace
channel associations with full schema detail.
```
