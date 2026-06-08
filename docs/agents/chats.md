---
slug: chats
title: Chats - multi-turn conversations with agents
summary: How chat turns are claimed, run, parked, cancelled, and resumed; the WS protocol and message-stream contract; auto-compaction.
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
the WebSocket message stream, `POST /v1/chats/{id}/cancel`, and so on)
and the operator console. They are NOT a system entity and there is
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
yield-pause-and-resume (an `ask_user` inside the agent parks the
chat until a reply lands), cancellation mid-stream, and turn drain
loops (multiple queued user messages process FIFO until the queue
is empty).

## Mental model

A `Chat` row carries:
- `id`, `agent_id` (the agent that runs every turn).
- `turn_status` - `idle | claimable | running`. The next state
  transition.
- `claimed_by` - worker id when running.
- `parked_status`, `parked_event_key`, `parked_until` - yield state
  when a tool has paused the turn.
- `cancel_requested_at` - set when the operator clicks cancel.

A `ChatMessage` row is the unit of conversation history:
- `chat_id`, `seq` (monotonic per chat - gaps are impossible).
- `kind` - one of: `user_message`, `assistant_token`,
  `assistant_message`, `tool_call`, `tool_result`, `yielded`,
  `resumed`, `done`, `error`, `cancelled`, `compaction_marker`,
  `usage`.
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
- `running` → `idle` (with yielded marker) - a tool in the turn
  yielded. The `parked_*` fields are set. The chat is now waiting
  for an external event.
- `running` → `idle` (with cancelled marker) - operator cancelled.
- `running` → `idle` (with error marker) - worker crashed or the
  LLM errored.

Disconnecting the WebSocket doesn't affect any of these. The chat
row keeps its `turn_status`; the worker keeps running. Reconnect
replays.

Parked-then-resumed flow:

- The tool returns `Yielded(...)`. The drain loop's catch block
  writes a `yielded` message, sets `parked_*` fields on the chat
  row, releases the lease.
- External event fires (channel reply, trigger fire). The publisher
  marks the chat resumable.
- A worker claims, calls the tool's `resume()`, gets back the
  result. Writes a `resumed` row + the synthetic `tool_result` row.
- Continues the agent loop in the same turn - the LLM sees the
  result and proceeds.

Cancellation:
- Operator POSTs `/v1/chats/{id}/cancel`. The handler sets
  `cancel_requested_at`.
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
- `GET /v1/chats/{id}` - fetch the row (turn_status, parked state,
  cancel state).
- `GET /v1/chats/{id}/messages?cursor=<seq>` - paginated messages
  by seq (the cursor query used for replay).
- WebSocket `/v1/chats/{id}/ws` - send a `user_message` and stream
  the assistant response. This is the actual "send and wait"
  interaction; it requires the WS handshake.
- `POST /v1/chats/{id}/cancel` - request cancellation of the
  current turn.

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

**Goal.** Agent wants to know if chat `ch-foo` is currently parked
on `ask_user` so it can post the answer.

1. Fetch the chat row over REST (`GET /v1/chats/ch-foo`).

Returns:
```json
{
  "id": "ch-foo",
  "agent_id": "support-bot",
  "turn_status": "idle",
  "parked_status": "parked",
  "parked_event_key": "ask_user:sid-A:tc-12",
  "parked_until": "2026-06-03T17:00:00Z"
}
```

2. The `parked_event_key` reveals: this chat is waiting for an
   `ask_user` reply against `sid-A:tc-12`. To resume it, route a
   reply through the channel system (or whatever path the operator
   set up).

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
  terminal.** A user sending five messages while the chat is parked
  produces five turns in order on resume - not one merged turn,
  not "drop all but the latest".
- **Approval pending + new user_message = approval auto-rejected.**
  The new turn supersedes the pending approval. Operators see this
  in the chat history as a `tool_result` with `rejected: superseded
  by new user input`.
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
- [yielding](yielding.md) - tool yields pause chat turns the same
  way they pause sessions.
- [tool-approval](tool-approval.md) - approval prompts park
  chats; new user turns supersede pending approvals.
- [channels](channels.md) - channel-forwarded `ask_user` and
  approval prompts unpark chats from external messaging.
