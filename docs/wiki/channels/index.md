---
title: "Channels"
summary: "Connect Slack, Telegram and Discord: providers, channels, inbound bindings and reply bindings."
order: 80
---

Channels connect primer to Slack, Telegram and Discord. These pages cover registering a provider (admin), adding channels, routing inbound messages to agents with bindings, and sending a workspace's questions and approvals to a channel. Start with [Channels](channels/channels).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`channel_providers__find`](reference/tools/channel_providers/find) | Find ChannelProvider with predicate. | admin |
| [`channel_providers__get_access`](reference/tools/channel_providers/get_access) | Who ChannelProvider is shared with. | user |
| [`channel_providers__put_access`](reference/tools/channel_providers/put_access) | Change who ChannelProvider is shared with (owners only). | user |
| [`channels__find`](reference/tools/channels/find) | Find Channel with predicate. | user |
| [`channels__get_access`](reference/tools/channels/get_access) | Who Channel is shared with. | user |
| [`channels__put_access`](reference/tools/channels/put_access) | Change who Channel is shared with (owners only). | user |
