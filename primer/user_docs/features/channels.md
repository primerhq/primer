---
slug: channels
title: Channels
section: features
summary: Multi-platform messaging - Slack, Discord, Telegram - with providers, channels, and per-agent associations.
---

## The three-level model

Channels wire primer to a messaging platform. Three nouns to keep
straight:

- **Channel provider**: the platform-level binding (a Slack
  workspace, a Discord guild, a Telegram bot). One per platform
  connection.
- **Channel**: a specific destination inside the provider (a
  Slack channel id, a Discord text channel, a Telegram chat).
- **Association**: ties an agent to a channel so the agent's
  output lands there.

A primer instance can have many providers, each with many
channels, each with many associations. The agent declares the
channels it speaks on at create time (or via PATCH later).

## What it looks like

The same `ask_user` prompt renders differently per platform.
Slack is white-card flat; Discord is dark-themed embed; Telegram
is a bubble + inline keyboard.

```mockup:channels-prompt
{ "platform": "slack", "question": "Approve last night's deploy?", "options": ["Approve", "Roll back"], "agentName": "release-bot" }
```

```mockup:channels-prompt
{ "platform": "discord", "question": "Approve last night's deploy?", "options": ["Approve", "Roll back"], "agentName": "release-bot" }
```

```mockup:channels-prompt
{ "platform": "telegram", "question": "Approve last night's deploy?", "options": ["Approve", "Roll back"], "agentName": "release-bot" }
```

## Adding a provider

Each provider kind has its own OAuth or token-based onboarding.
The console walks the operator through it; the REST surface is
symmetric:

```code-tabs:python,curl
--- python
prov = client.channels.create_provider(
    kind="slack",
    name="ops-slack",
    config={"bot_token": "xoxb-..."},
)
chan = client.channels.create(
    provider_id=prov.id,
    channel_id="C0123456789",
    name="ops-alerts",
)
client.channels.associate(agent_id="release-bot", channel_id=chan.id)
--- curl
curl -X POST https://primer.example/v1/channel_providers \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"kind":"slack","name":"ops-slack","config":{"bot_token":"xoxb-..."}}'
```

## Rate limits

Each platform throttles app messages. Primer batches and backs
off when the platform returns a 429; the agent does not see the
throttle directly, but a chronically-throttled agent may take
seconds longer per turn than it would otherwise.

```callout:warning
Slack rate-limits app messages at ~1 per second per channel. If
your agent posts intermediate progress lines, expect them to
serialise. The trigger run logs surface the throttle delay so
you can spot 'why is this agent slow' issues.
```
