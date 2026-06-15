---
slug: channels-overview
title: Channels
section: channels
summary: Channels connect primer to messaging platforms (Slack, Telegram, Discord) so people can talk to agents and answer their questions from chat.
---

## What channels give you

A **channel** connects primer to a messaging platform so agents are reachable where people already are. Channels carry two directions of traffic:

- **Access**: messages from a platform reach an agent, and the agent's replies go back to the same conversation.
- **Interactivity**: when a session asks a question or hits an approval gate, the channel forwards that prompt to chat and relays the human's answer back, so a run can be driven entirely from Slack, Telegram, or Discord.

A **channel provider** holds the platform connection and credentials; a **workspace association** decides which workspace (and which agents) a channel routes to.

```ref:channels/channel-providers
Register a Slack, Telegram, or Discord channel provider and its credentials.
```

```ref:channels/channels
How channels drive chats and forward a session's questions and approvals to a conversation.
```

```ref:channels/channel-workspace-association
Bind a channel to a workspace and scope which agents it can reach.
```
