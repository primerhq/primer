---
slug: channels-overview
title: Channels
section: channels
summary: Channels connect primer to messaging platforms (Slack, Telegram, Discord), map inbound events to platform actions, and route a session's replies back to the conversation.
---

## What channels give you

A **channel** connects primer to a messaging platform so agents are reachable where people already are. A channel is one set of credentials and one room, used by two independent surfaces:

- **Inbound: channel event to platform action.** A message or command arriving in the room is normalized into a `ChannelEvent` and matched against rules. A match dispatches an action: start a chat, run an agent or graph session, or resume a parked workflow. The default "a new message opens a chat" still works with no rule at all.
- **Outbound: session reply to channel.** When a session asks a question, hits an approval gate, or finishes, that traffic follows a reply binding back to the channel and thread it came from. The standing reply binding lives on the workspace; an inbound rule can set a more specific one per session.

A **channel provider** holds the platform connection and credentials; **channel rules** map inbound events to actions; a **reply binding** decides where a workspace's session traffic goes back.

```ref:channels/channel-providers
Register a Slack, Telegram, or Discord channel provider and its credentials.
```

```ref:channels/channels
How channels drive chats and the per-room chat settings.
```

```ref:channels/channel-rules
Map inbound events to platform actions and choose the reply target.
```

```ref:channels/channel-reply-binding
Bind a workspace to a channel as its standing outbound reply destination.
```
