---
slug: chats
title: Chats
section: features
summary: The chats list, real-time streaming, attachments, sharing, and the WebSocket client contract.
---

## The chats list

The Chats page shows every chat thread the operator (or another
agent) is participating in. Each row carries the bound agent, the
last message preview, and a timestamp. Clicking a row opens the
chat in the streaming view.

## The streaming view

The chat view streams assistant messages over a WebSocket as the
agent produces them. The cursor under the streaming message shows
the tokens landing in near real time.

```mockup:chat-stream
{ "chatId": "chat-x9y8z7", "streaming": true, "userName": "alex", "agentName": "helper" }
```

The streaming dots disappear when the turn completes. If the
client disconnects mid-turn the agent keeps running; reconnecting
replays the messages that landed during the disconnect.

```callout:info
The WebSocket is a delivery channel only; persistence is in the
ChatMessage rows. Disconnects do not cancel turns. Use the
explicit cancel endpoint when you want a turn to stop.
```

## Sending a message

Three transports get you to the same place.

```code-tabs:python,curl,javascript
--- python
client.chats.send(chat_id="chat-x9y8z7", content="What's next on the queue?")
--- curl
curl -X POST https://primer.example/v1/chats/chat-x9y8z7/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content":"What is next on the queue?"}'
--- javascript
const ws = new WebSocket(`wss://primer.example/v1/chats/chat-x9y8z7/ws`);
ws.onopen = () => ws.send(JSON.stringify({
  kind: "user_message",
  content: "What is next on the queue?",
}));
ws.onmessage = (ev) => console.log(JSON.parse(ev.data));
```

The WS variant streams the assistant reply token by token. The
REST variant returns the assembled reply once the turn completes.

## Attachments

Drag a file into the message input to attach it. Attachments land
in the workspace under `inbox/` and become available to
filesystem tools. The agent sees a synthetic message describing
the attachment.

## Sharing

Each chat has a per-thread share token. Mint one from the chat's
overflow menu; anyone with the URL can read the transcript (no
writes). Revoke from the same menu.

```callout:info
Shared chats are read-only by design. There is no per-share write
permission; if you need a collaborator to drive the chat, give
them an operator account.
```
