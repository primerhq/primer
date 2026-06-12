---
slug: chats
title: Chats
section: features
summary: Start and use a chat in the console -- open a conversation with an agent, send messages, watch token streaming, and approve gated tool calls.
---

## Overview

A chat is a long-running, multi-turn conversation with a single agent. The
console gives you a live streaming view of every token the agent produces,
inline tool-approval cards when a tool call needs your sign-off, and a
persistent message log you can scroll back through at any time.

```ref:concepts/chats
Background on the turn shape, message log, compaction, and how chats
differ from sessions.
```

## Open a chat

1. Go to **Chats** in the left nav. The list shows every existing chat thread
   with its bound agent, status pill (active / ended), message count, and
   creation time.

2. Click **New chat** (top-right of the filter bar). A modal opens:
   - **Agent** -- select the agent this chat is bound to. The dropdown
     shows all registered agents. You must have at least one agent before
     you can create a chat.
   - **Initial instructions** (optional) -- free-text guidance sent ahead
     of the first user message so the agent has context.

3. Click **Create chat**. The console navigates straight to the streaming
   view for the new chat.

To re-open any existing chat, click its row in the list.

## Send a message and watch streaming

The chat detail panel is split into a scrollable message log above and a
composer at the bottom.

```embed:chat-stream
```

1. Type your message in the composer textarea. Press **Enter** (or
   **Shift+Enter** for a newline) or click **Send**.

2. The agent label appears on the left of the message log. Tokens land
   token-by-token; the **Thinking...** indicator appears between your
   message and the first delta if there is a brief gap while the worker
   picks up the turn.

3. The header shows:
   - The chat id and the bound agent.
   - A **TokenMeter** pill showing input tokens / context length. Click
     the compress icon next to it to trigger a manual compaction pass.
   - A **live / connecting / offline** badge for the WebSocket state.
   - The chat status pill (active / ended).

4. Scroll up at any time to load older messages. The console pages backward
   through the message log without losing your scroll position.

```callout:info
If the WebSocket drops mid-turn the agent keeps running. When you reconnect,
the console replays every message that arrived during the gap -- nothing is
lost. The Send button is re-enabled once the socket is back to "live".
```

## Switching the agent

A chat is not locked to the agent it was created with. You can change the agent
mid-conversation using the agent dropdown in the chat header (or the
`POST /v1/chats/{id}/agent` endpoint). Pick a different agent and the switch
takes effect on the next turn.

The conversation history is preserved across the switch. The new agent sees the
full prior exchange as context; only the system prompt and the available tools
change from the next turn onward. Nothing in the message log is rewritten or
replayed under the new agent.

If a tool approval or an `ask_user` question is pending when you switch, it is
auto-rejected first so the conversation can hand off cleanly. The pending card
clears and the agent receives a rejection result for that call. Switching to the
agent the chat is already bound to does nothing.

## Attach a file

Click the paperclip button to the left of the composer, or drag and drop
a file onto the chat panel. Images and PDFs up to 8 MiB are accepted. An
attachment chip appears in the strip above the composer showing a thumbnail
(images) or a file icon with the name and size (documents). Click the x
on any chip to remove it before sending.

## Approve a gated tool call

When the agent calls a tool covered by a `required` approval policy, the
turn parks and an inline approval card appears above the composer showing the
tool name and its arguments.

1. Review the tool name and arguments in the card.
2. Click **Approve** to let the call proceed, or **Reject** and supply a
   reason. The rejection reason is passed back to the agent as context.

If you send a new message while an approval is pending, a warning banner
appears explaining that the server will auto-reject the parked tool call when
your new message arrives. Confirm with **Send and reject** or cancel.

```callout:warning
Rejection is not a retry. The agent receives a rejection result and decides
how to continue on its own. If you want the tool to run, approve it -- do not
send a new message first.
```

## Filter the chats list

The filter bar at the top of the Chats list accepts a free-text query (matches
on chat id, agent id, and title) and an agent dropdown. Filters combine: you
can search for a keyword scoped to one agent. Clear filters by erasing the
text field and resetting the agent dropdown to "all agents".

## Delete a chat

Click the trash icon on any row in the chats list (right column) to delete
that chat. A confirmation modal appears. Deletion removes all persisted
messages and cannot be undone.

```ref:reference/api-chats
Automate this -- the API reference covers creating chats, fetching message
logs, the WebSocket frame protocol, and the tool-approval endpoints.
```
