---
slug: chats-concept
title: Chats
section: concepts
summary: An interactive multi-turn conversation surface bound to one agent, with message streaming, tool approval, and automatic compaction.
---

## What a chat is

A chat is an interactive, multi-turn conversation bound to a single agent.
It is the right primitive when there is a human (or another agent) keeping
the loop going turn by turn. The operator sends a message, the agent
responds, the operator replies, and this continues indefinitely on the same
persistent message log.

Unlike a session -- which runs headless under a scheduler and does work
autonomously -- a chat waits for the next human message before starting
a new turn.

```callout:tip
Pick chat for interactive back-and-forth. Pick session for autonomous,
long-running work where the agent should proceed without waiting.
```

## The turn shape

A chat turn starts when a user message arrives and ends when the agent
produces a final reply. Between those two events, the agent may call tools
zero or more times:

```mermaid
sequenceDiagram
    participant User
    participant Chat
    participant Worker
    participant Agent

    User->>Chat: user message
    Chat->>Chat: persist message; mark claimable
    Worker->>Chat: claim turn
    Agent->>Worker: stream reply + tool calls
    loop tool round-trips
        Worker->>Agent: dispatch tool
        Agent->>Worker: tool result
    end
    Worker->>Chat: persist final reply
    Chat-->>User: stream complete
```

The critical design property is **turn detachment**: the user message is
persisted to storage the moment it arrives, and a background worker claims
the turn independently. The connection the message arrived on has no bearing
on whether the turn runs. If the client disconnects mid-turn, the agent
keeps working. When the client reconnects, it replays the messages that
landed during the gap.

## The message log

Every chat maintains an ordered, append-only log of `ChatMessage` rows.
Each row has a monotonic sequence number that doubles as a replay cursor.
Message kinds include `user_message`, `assistant_token`, `tool_call`,
`tool_result`, `done`, `cancelled`, `error`, `yielded`, `resumed`, and
`compaction_marker`.

The log is the source of truth. Live streaming is advisory: the client
receives ticks that tell it new rows are available and re-reads from
storage on each tick.

## Tool approval over chat

When an agent in a chat calls a tool covered by a `required` approval policy,
the chat parks on that tool call. The pending call and its arguments become
visible, and the turn does not continue until an operator approves or rejects
the call.

Rejection is not a retry. The agent receives a clean error and decides how
to proceed. A pending approval is also auto-rejected if the operator sends
a new message or an interrupt while the approval is outstanding.

## Compaction

A chat's message log grows with every turn. When token usage approaches the
model's context limit, primer automatically compacts the oldest turns into a
single summary message. The original rows are kept in the log for audit and
replay, but the next turn's prompt is assembled from the summary plus the
recent turns only.

Compaction happens automatically at the start of each turn when the context
is near capacity. The result appears in the message log as a
`compaction_marker` row, and the streaming client receives a `compaction`
envelope describing the token savings.

## Chat vs session

| | Chat | Session |
|---|---|---|
| Who drives each turn | A human or external agent sends a message | The scheduler; agent runs autonomously |
| Stopping point | Waits for the next user message | Runs until done, waiting, or paused |
| History store | Ordered `ChatMessage` rows in the database | `messages.jsonl` inside the workspace |
| Workspace | No workspace attached | Always scoped to one workspace |
| Compaction marker | Structured `compaction_marker` row | Prefix string in `messages.jsonl` |

Both surfaces use the same agent loop, tool-dispatch machinery, approval gate,
and park-resume protocol for yielding tools.

```ref:features/chats
The feature walkthrough covers creating a chat and the streaming message UI.
```

```ref:reference/api-chats
The API reference documents all chat fields, the message log endpoints, and
the WebSocket frame protocol.
```
