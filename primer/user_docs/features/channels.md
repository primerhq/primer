---
slug: channels
title: Channels
summary: Connect Slack, Telegram, or Discord to primer by configuring a provider, creating channels, and binding them to workspaces.
section: features
---

## Overview

Channels wire primer to a messaging platform. Three objects to keep straight:

- **Channel provider**: the platform-level binding -- a Slack workspace, a Discord guild, or a Telegram bot. One per platform connection.
- **Channel**: a specific destination inside the provider -- a Slack channel ID, a Discord text channel snowflake, or a Telegram chat ID.
- **Association**: binds a workspace to a channel so the workspace's agents can send and receive messages there.

A primer instance can have many providers, each with many channels, each linked to many workspaces.

```embed:channels
```

## Add a channel provider

1. Navigate to **Channels** in the sidebar, then open the **Providers** tab.
2. Click **New provider**.
3. Select a **platform** (Slack, Telegram, or Discord). The platform field is locked after creation -- recreate the provider to switch platforms.
4. Fill in the platform-specific credentials:
   - **Slack**: App token (`xapp-...`) and Bot token (`xoxb-...`) are both required. The App token is found in the Basic Information panel of your Slack app. The signing secret field is optional and only needed for HTTP delivery mode.
   - **Telegram**: Bot token in `<id>:<hash>` format (at least 20 characters), obtained from `@BotFather`. The poll timeout defaults to 25 seconds.
   - **Discord**: Bot token from the Discord Developer Portal. Do not include the `Bot ` prefix. Enable DMs if you want the bot to handle direct messages.
5. Optionally supply an **ID** for the provider. Leave blank for an auto-generated one.
6. Click **Create provider**. The console navigates to the provider detail page.

```callout:warning
Slack requires two tokens with distinct prefixes. The app token must start with `xapp-` and the bot token must start with `xoxb-`. Mixing them up or using a token with the wrong prefix causes authentication failures that are not obvious from the platform error message.
```

## Add a channel

1. Open the **Channels** tab. The **New channel** button is disabled until at least one provider exists.
2. Click **New channel**.
3. Select the **provider** the channel belongs to. This field is locked after creation.
4. Enter the **external ID** of the specific conversation:
   - Slack: channel ID (e.g. `C0123ABC456`)
   - Telegram: chat ID (numeric)
   - Discord: channel snowflake
5. Optionally enter a **label** (up to 200 characters, e.g. `#ops-alerts`) for display purposes.
6. Click **Create channel**.

## Bind a channel to a workspace

1. Open the **Associations** tab.
2. Click **New association**. Both at least one workspace and one channel must exist.
3. Select the **workspace** and the **channel**.
4. Configure the three flags:
   - **Enabled**: when off, no fan-outs are routed to this channel.
   - **Forward ask_user**: route channel-mediated user prompts to this channel.
   - **Forward tool_approval**: route channel-mediated tool approval requests to this channel.
5. Click **Create**. The association row appears in the table with toggle controls for each flag.

## Automate this

```ref reference/api-channels
The API reference covers channel providers, channels, and workspace channel associations with full schema detail.
```

```ref concepts/chats
Chats explains how agents interact with users through conversations, which channels can carry.
```
