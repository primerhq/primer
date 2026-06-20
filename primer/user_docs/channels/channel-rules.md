---
slug: channels-rules
title: Channel rules
section: channels
summary: Map normalized channel events (a message or a command) to a platform action, and choose where the reply goes back.
---

## Two surfaces, one set of rules

A channel carries two directions of traffic, and a rule shapes each one:

- **Inbound: event to action.** A message or command arriving on the room is normalized into a `ChannelEvent`, matched against a predicate, and dispatched to an action (start a chat, run a session, resume a parked session). This is what the rule editor authors.
- **Outbound: reply binding.** Whatever a session sends back (a question, an approval prompt, the final result) follows a reply binding to a channel and thread. A rule's reply target sets the outbound destination for the action it starts.

Both run on the same room. You author inbound rules here and pick the reply target for each one; the standing outbound destination for a workspace is the reply binding on the workspace itself.

## Normalized events

Every provider's raw events (Slack, Telegram, Discord) are normalized into one envelope so a rule never has to know platform specifics. The v1 core event types are:

| Event type | Meaning |
|---|---|
| `message.posted` | A user posts text or media in the room. |
| `command.invoked` | A user runs a slash or bot command, with its name and arguments. |

The richness lives in the envelope dimensions a matcher keys off, not in a long list of types. A matcher is the AND of whatever fields you set; omit a field and it is unconstrained:

| Dimension | What it matches |
|---|---|
| `event_type` | Required. `message.posted` or `command.invoked`. |
| `surface` | A subset of `dm`, `channel`, `thread` (where the message landed). |
| `mentions_bot` | Whether the message mentions the bot. |
| `command_name` | The command name for a `command.invoked` event (for example `deploy`). |
| `sender_roles_any` | Match when the sender holds any of these roles. |
| `sender_ids_any` | Match when the sender is one of these platform user ids. |
| `text_pattern` | A regular expression the message text must contain. |
| `room_external_ids` | Restrict to specific platform room ids. |

## Actions

When an event matches, the rule dispatches one action:

| Action | What it does |
|---|---|
| Start a new chat | Open a fresh chat bound to the source thread, seeded with the event text, and relay the reply back. |
| Reply to a chat | Append the event text to an existing chat (named directly, or resolved from the source thread's bound chat). |
| Run a session | Spin up a fresh agent or graph session in a workspace; the reply target becomes that session's reply binding (full-lifecycle relay). |
| Resume a parked session | Wake a session that is parked waiting on a channel event (the runtime subscribe path; for example a slash command resumes a workflow). |

## Capabilities and prerequisites

Each provider can emit only the events it is configured for, and some require platform setup. The rule editor offers the supported events per provider and surfaces these prerequisites as warnings:

- **Discord** needs the MESSAGE CONTENT privileged intent enabled in the bot's settings before it can read message text.
- **Telegram** needs privacy mode disabled in BotFather (or the bot made a group admin) to receive group messages; direct messages always arrive.
- **Slack** needs the matching event subscriptions plus bot scopes (`chat:write`, `channels:read`, `channels:history`).

A missing prerequisite is a warning, not a hard failure: the rule saves, but the event may never arrive until the platform side is configured.

## Reply targets

A rule's reply target decides where the action's outbound traffic goes:

| Reply target | Where the reply lands |
|---|---|
| `source_thread` (default) | The thread the event came from. |
| `source_room` | The room root, not a thread. |
| `dm_sender` | A direct message to the person who triggered the event. |
| `none` | No outbound reply; the action runs silently. |

For session actions the resolved reply target becomes a session-scoped reply binding anchored to the originating thread, so the session feels conversational and a specific event rule wins over the workspace's standing binding.

```ref:channels/channels
Channel rooms, chat settings, and in-channel commands.
```

```ref:channels/channel-providers
Platform credentials and provider setup, including the required scopes and intents.
```
