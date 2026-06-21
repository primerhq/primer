---
slug: chats
title: Chats - multi-turn conversations with agents
summary: How chat turns are claimed, run, soft-yielded, cancelled, and resumed; the WS protocol and message-stream contract; auto-compaction.
related: [agents, yielding, tool-approval, channels]
# Chats are NOT available over MCP; they are driven via the REST API
# and the operator console. For headless MCP execution use a session
# (see sessions.md). No chat MCP tools exist.
mcp_tools: []
---

# Chats - multi-turn conversations with agents

## Overview

A **Chat** is a multi-turn conversation between a human (or another
agent) and a primer agent. It's the interactive, human-in-the-loop
surface: the operator console renders chats as a familiar chat UI; an
external client connects via WebSocket to send messages and stream
assistant responses. The whole conversation is a series of
`ChatMessage` rows persisted in order; the wire protocol is just
"send a user_message; replay messages on connect; stream new ones
as they arrive."

Chats are created and driven through the REST API (`POST /v1/chats`,
the WebSocket message stream including its `interrupt` cancel signal,
and so on) and the operator console. They are NOT a system entity and there is
NO chat MCP toolset, so an external MCP-connected agent cannot create
or drive a chat directly.

Use a chat when a human (or another agent) drives a back-and-forth
conversation turn by turn. When the work runs headless to completion
with no one in the loop, or when you want to run an agent or graph
programmatically over MCP, use a [session](sessions.md) instead
(`workspaces::create_workspace_session`) - that is the only MCP-driven
way to run an agent or graph headlessly.

Under the hood chats are deliberately not bound to a WebSocket. A
turn - the work of "given the user_message, run the agent loop
until a non-tool stop" - runs as a worker pool job. The WebSocket is
just a viewport: connecting attaches to the message stream; sending
a user_message triggers a claim; disconnecting doesn't pause
anything; reconnecting replays from a cursor and resumes streaming.
This decoupling is what lets a long-running chat survive browser
refreshes, mobile-app context switches, and the user closing their
laptop.

Chats are also where most of primer's complex execution semantics
land: auto-compaction (history gets summarised at 90% context),
soft-yield (an `ask_user` or approval inside the agent ends the turn
conversationally and waits for the human's next message, rather than
parking), cancellation mid-stream, and turn drain loops (multiple
queued user messages process FIFO until the queue is empty).

## Mental model

A `Chat` row carries:
- `id`, `agent_id` (the chat's current agent; it runs the next turn
  and is switchable mid-chat via `POST /v1/chats/{id}/agent`).
- `turn_status` - `idle | claimable | running`. The next state
  transition.
- `pending_tool_call` - the soft-yield gate. A chat never parks: when
  a tool yields (`ask_user` or an approval gate) the turn ends
  conversationally and the pending call is recorded here, to be
  resolved by the human's next message (consumed as that call's
  `tool_result`).
- `pending_handoff` - a queued agent switch, applied at the next turn.
- `cancel_requested_at` - set when cancellation is requested.

A `ChatMessage` row is the unit of conversation history:
- `chat_id`, `seq` (monotonic per chat - gaps are impossible).
- `kind` - one of: `user_message`, `assistant_token`, `tool_call`,
  `tool_result`, `done`, `cancelled`, `error`, `compaction_marker`.
  (`yielded` and `resumed` exist in the enum but are never written on
  the chat soft-yield path, since chats never park.)
- `payload` - kind-specific JSON.

The high-level turn pipeline:

1. WebSocket `recv_loop` accepts `{"kind": "user_message", "content":
   "..."}`. Persists a row with `kind=user_message`. Sets
   `turn_status=claimable`.
2. The `Scheduler.claim_chats()` query finds the row and a worker
   claims it.
3. The worker's `_run_one_chat_turn` enters a drain loop:
   - Finds `last_terminal_seq` (the seq of the most recent
     `done/error/cancelled/yielded`).
   - Looks for the next `user_message` with seq > last_terminal_seq.
   - If found, runs the agent loop: stream tokens, persist messages,
     dispatch tools, etc.
   - When the agent stops with a non-tool message, write a `done`
     row.
   - Loop back. If no more queued user_messages, exit drain.
4. WebSocket `send_loop` (on the connected client side) replays via
   the cursor query, then streams live ticks.

The cursor query: `GET /v1/chats/{id}/messages?cursor=<seq>` returns
every row with seq > cursor in order. The client sets cursor to the
latest seq it has seen; on reconnect, this fills the gap and the
client is back in sync.

A turn's history input to the LLM is reconstructed on every turn -
walk all messages, transform into the LLM's message format, apply
any `compaction_marker` to elide rows in the marker's range and
substitute the summary.

## Lifecycle and states

`turn_status` transitions:

- `idle` → `claimable` - a new `user_message` row written. Scheduler
  query catches it on the next tick.
- `claimable` → `running` - worker claimed.
- `running` → `idle` - drain loop exited (no queued user_messages
  left). Written as a `done` row.
- `running` → `idle` (soft-yield) - a tool yielded (`ask_user` or an
  approval gate). The turn ends conversationally and
  `pending_tool_call` is recorded; the chat waits for the human's
  next message rather than parking.
- `running` → `idle` (with cancelled marker) - operator cancelled.
- `running` → `idle` (with error marker) - worker crashed or the
  LLM errored.

Disconnecting the WebSocket doesn't affect any of these. The chat
row keeps its `turn_status`; the worker keeps running. Reconnect
replays.

Soft-yield-and-resume flow:

- The tool raises a yield (`ask_user` or an approval gate). The
  dispatcher records `pending_tool_call` on the chat row and ends the
  turn conversationally (the question or approval prompt is the last
  assistant message). The chat goes `idle` - no park slot, no held
  lease.
- The human's next `user_message` is consumed as the pending call's
  `tool_result` (an approval reply is parsed as approve/reject).
- The next turn continues the agent loop with that result in history -
  the LLM sees the answer and proceeds.

Cancellation:
- The client sends an `interrupt` message over the chat WebSocket.
  The handler sets `cancel_requested_at` and publishes a cancel event.
- The worker's per-token loop checks for cancel between tokens. On
  detection: stop streaming, write a `cancelled` row, release the
  lease. Mid-tool-call: tool runs to completion (we don't kill
  external HTTP calls partway through), result is recorded, then
  the cancel takes effect.
- A user_message arriving during a pending cancellation is queued
  normally; once the cancel writes its terminal marker, the drain
  loop picks up the queued message and starts a new turn.

Auto-compaction:
- Before each LLM call, `should_compact()` runs. Counts tokens in
  the history, compares against `context_length * trigger_ratio`
  (default 0.90). If over: apply the compaction strategy.
- Default strategy: summarise the head of the history with an LLM
  call; keep the tail. Write a `compaction_marker` row with
  `replaced_from_seq..replaced_to_seq` and the summary text.
- Next time the history is reconstructed, the marker rewrites the
  range: synthetic message with summary text replaces the elided
  rows.

## How chats are driven (REST + console, not MCP)

There is NO chat MCP toolset. Chats are not a system entity exposed
over MCP; they are the interactive surface managed through the REST
API and the operator console:

- `POST /v1/chats` - create a chat. Body needs `agent_id` and an
  optional initial `user_message` content.
- `GET /v1/chats/{id}` - fetch the row (turn_status, pending-gate
  state, cancel state).
- `GET /v1/chats/{id}/messages?cursor=<seq>` - paginated messages
  by seq (the cursor query used for replay).
- WebSocket `/v1/chats/{id}/ws` - send a `user_message` and stream
  the assistant response. This is the actual "send and wait"
  interaction; it requires the WS handshake.
- WebSocket `interrupt` message - request cancellation of the
  current turn (sets `cancel_requested_at` + publishes a cancel
  event). There is no REST cancel route; cancellation rides the same
  WebSocket. (`POST /v1/chats/{id}/agent` switches the chat's agent,
  and `POST /v1/chats/{id}/compact` forces a compaction.)

For an external agent connected over MCP, there is no way to create
or drive a chat directly. Two honest options:

- To run an agent or graph headlessly over MCP, use a
  [session](sessions.md) (`workspaces::create_workspace_session`)
  instead. That is the MCP-native primitive.
- To push a message into an existing chat without a WS handshake,
  create a chat-message subscription on a trigger and fire the
  trigger. The fire dispatcher appends the rendered payload to the
  chat exactly as if a user had sent it. See
  [triggers-and-subscriptions](triggers-and-subscriptions.md).

## Workflows

### Workflow 1 - agent inspects an in-flight chat

**Goal.** Agent wants to know if chat `ch-foo` is waiting on an
`ask_user` answer so it can post the reply.

1. Fetch the chat row over REST (`GET /v1/chats/ch-foo`).

Returns:
```json
{
  "id": "ch-foo",
  "agent_id": "support-bot",
  "turn_status": "idle",
  "pending_tool_call": {"...": "the ask_user call awaiting a reply"}
}
```

2. A non-null `pending_tool_call` means the chat is mid-soft-yield:
   the agent asked a question and the turn ended. To answer, send the
   reply as the next `user_message` (over the WebSocket, or via a
   channel-forwarded reply); it is consumed as that call's
   `tool_result` and the agent continues.

### Workflow 2 - agent queues work for another chat via trigger

**Goal.** Agent observes that chat `ch-customer-42` should receive a
status update. It can't WS into the chat from MCP. Solution: fire a
trigger configured to post to that chat.

(Assumes trigger `tg-status-update` with subscription
kind=`chat_message`, config={`chat_id`: "ch-customer-42"}.)

1. Update the payload template if needed:

```json
{
  "tool": "trigger::update",
  "arguments": {
    "id": "tg-status-update",
    "payload_template": "Status: {{ overrides.text }}"
  }
}
```

2. Fire it with the message in overrides:

```json
{
  "tool": "trigger::fire_now",
  "arguments": {
    "id": "tg-status-update",
    "overrides": {"text": "Order shipped, ETA Friday."}
  }
}
```

The subscription dispatcher appends a `user_message` row to
`ch-customer-42`. The chat's drain loop picks it up; the
`support-bot` agent processes it and replies.

## Gotchas

- **The WS connection is not the chat.** Disconnects don't stop
  turns. Reconnects don't re-trigger them. Workers keep running.
  The cursor protocol fills any gap. Code that conflates "WS
  connected" with "turn running" gets confused.
- **Drain is FIFO across all queued user_messages since the last
  terminal.** A user sending five messages while a turn is in flight
  produces five turns in order on drain - not one merged turn,
  not "drop all but the latest".
- **A pending gate is resolved by the next user message.** When the
  agent is mid soft-yield on an `ask_user` or approval, the human's
  next message is consumed as that call's `tool_result` (an approval
  reply is parsed as approve/reject). Switching the chat's agent also
  auto-resolves a pending gate first.
- **Cancellation is between tokens.** Mid-tool-call cancellation
  lets the tool finish (we don't interrupt external HTTP). The
  cancel takes effect at the next LLM-token boundary.
- **Auto-compaction can rewrite history between turns.** A turn
  that starts at message 100 might see, at LLM-input time, a
  reconstructed history with messages 50-89 elided and replaced by
  a synthetic summary. The agent should not assume the history it
  sees is a contiguous slice of stored messages.
- **Sweeper reclaims stale leases.** A worker that's stopped
  heartbeating for >90s gets its lease swept. The chat row gets a
  synthetic `error` row written. The next claim picks it up.
- **WS replays don't re-process tokens.** Reconnect's cursor replay
  delivers historical rows as-is; the client UI re-renders them.
  No tools run again, no LLM is called.
- **`chat_message` triggers append `user_message` rows.** They
  don't append assistant messages or skip the agent - the fire
  feeds into the regular drain loop. So firing a trigger to "say
  hello" actually invokes the agent again.

## Related

- [agents](agents.md) - every chat is bound to an agent that runs
  each turn.
- [yielding](yielding.md) - tool yields end a chat turn
  conversationally (soft-yield), unlike sessions, which park.
- [tool-approval](tool-approval.md) - approval prompts soft-yield
  chats; the next user message resolves them.
- [channels](channels.md) - channel-forwarded replies resolve a
  chat's pending `ask_user` / approval gate.
