---
slug: yielding
title: Yielding tools
summary: How tools park an agent's session indefinitely on an external event and resume when the event fires.
related: [tool-approval, triggers-and-subscriptions, channels, sessions]
mcp_tools: []
---

# Yielding tools

## Overview

A **yielding tool** is a tool whose handler returns a `Yielded`
sentinel instead of a normal result. When the tool execution manager
sees that sentinel, it doesn't dispatch and wait - it parks the
calling session or graph node into storage, releases the worker
lease, and exits. The session stays parked, consuming zero compute,
until some external event fires the event key the yield is registered
against. A worker then claims the resumable row, calls the tool's
`resume()` hook with the event payload, and the tool produces the
result the agent will see on its next turn.

This is the primitive behind every "wait for the user to do X" tool
in primer. In a session or graph, `ask_user` parks on an
`ask_user:{sid}:{tcid}` key (`ask_user:{sid}:{node}:{tcid}` inside a graph
node: two fan-out siblings can share a `tcid`) and resumes when the user replies in a
channel. `subscribe_to_trigger` parks on a `trigger:{sid}:{trigger_id}` key
(the session is in the key: two sessions subscribed to one trigger do not share one) and resumes when the trigger fires. Tool approval reuses the same
primitive - a `_approval` yield parks the call until the operator's
decision arrives. In a session or graph the shape is always
identical: yield → park → external event → resume.

Yielding tools are powerful, but they're also unavailable over MCP.
The MCP transport has no pause/resume mechanism - `tools/call` is a
single request/response. If primer exposed a yielding tool to an
MCP client, the response would either time out or hang the client
forever. Primer's MCP `is_exposable()` gate therefore drops every
yielding tool from `tools/list`. Inside primer's own agent runtime
(when a primer-hosted agent calls a tool during a session
turn), yields work normally; outside, they aren't visible at all.

## Mental model

A yielding tool's handler returns:

```python
Yielded(
  tool_name="subscribe_to_trigger",  # the human-visible name
  event_key="trigger:sess-7c1e:tr-3f9a1c0b7d42",  # what we're waiting on
  timeout=300,                       # seconds; None = wait forever
  resume_metadata={"trigger_id": "tr-3f9a1c0b7d42", "subscription_id": "sb-5e8d2a6c9b10"},
)
```

The `event_key` is a free-form string. It's how the event bus matches
fires to parked sessions: when something publishes
`subscription_matched(event_key="trigger:sess-7c1e:tr-3f9a1c0b7d42")`, every session
parked on that key is marked resumable. The keys of `ask_user`, `timer`, `trigger`
and `watch` carry the session id, so each is one session's: a fire for another
session's key cannot reach it. A key that two sessions DO share resumes both.

The `resume_metadata` is the tool's own opaque state. It's stashed
on the session row and handed back to the tool's `resume()` hook
when the worker re-dispatches. Tools use it to remember which
specific record they were waiting on - e.g. a `subscribe_to_trigger`
needs to know which subscription to update on fire.

Session state during a park:

- `parked_status="parked"` - the high-level lifecycle position
  (`parked` | `resumable`).
- `parked_event_key="trigger:sess-7c1e:tg-foo"` - what's being waited on, for a
  single-event park. A multi-event park uses `parked_event_keys` (a
  list) instead.
- `parked_until=<timestamp>` - null if no timeout, else the deadline.
- `parked_at=<timestamp>` - when the park was recorded.
- `parked_state={...}` - the LLM message buffer, agent state, and the
  tool's opaque resume metadata needed to continue the turn after
  resume. Persisted as JSON so the resume worker can run on a
  different process from the parker.

When the event fires:
1. Whatever published the event calls `ClaimEngine.mark_resumable(
   "session", session_id, priority=50)`. The priority jumps the lease
   queue so resumes preempt fresh work.
2. A worker claims the session via the regular claim path.
3. The worker loads the parked state, calls `tool.resume(metadata,
   event_payload)`, and gets a normal `ToolResult` back.
4. The result is appended to the LLM's history; the agent loop
   continues from where it parked.

Crucial detail: **the agent definition is NOT snapshotted in the
park**. Only the LLM messages are persisted. When the session
resumes, the agent row is re-read from storage. So if an operator
edits the agent's prompt or tool set while the session is parked,
the edits take effect at resume time. This is sometimes what you
want (live config changes) and sometimes what you don't (a tool the
agent expected to have suddenly missing).

## Lifecycle and states

The session's `parked_status` field tracks where it sits:

- **(unset)** - normal operation. Not parked.
- **parked** - yield handler ran, session is in storage with no lease.
  Will not be picked up by workers until an external transition.
- **resumable** - the event fired. A lease is available for a worker
  to claim with priority 50.
- **(unset, after resume)** - worker has resumed and the tool's
  result is in the LLM buffer. The session is back in normal claim
  rotation.

The transitions:

- `→ parked` - tool returns `Yielded(...)`; tool manager raises the
  yield exception; worker park hook persists state and releases the
  lease.
- `parked → resumable` - external publisher calls
  `mark_resumable(kind, entity_id, priority=50)`. Three publishers
  in v1: trigger fire dispatcher, channel inbox (`ask_user` /
  `_approval` replies), and the timeout sweeper.
- `resumable → (running)` - worker claims via `ClaimEngine.claim_due()`.
- `→ end (cancelled)` - a yield cancellation: the agent's session is
  cancelled while parked. The tool's `resume()` is called with a
  `YieldCancelled` payload so it can clean up any external
  registrations. Cancelling the yield is distinct from cancelling
  the whole session - the session continues with a `tool_cancelled`
  result.
- `parked → (timeout-resumable)` - a periodic sweeper (every 30s)
  scans for `parked_until <= now()` and marks the matching rows
  resumable. The resume payload is a `YieldTimeout` marker; the
  tool's hook produces the right "I timed out" result for the LLM.

## MCP tools

Yielding tools as a class are not exposed over MCP. The specific
yields in primer:

- `system::ask_user` - pause to ask a question of the operator.
- `workspace_ext::subscribe_to_trigger` - wait for a named trigger to
  fire.
- `_approval` - internal yield used by tool approval (not a tool the
  agent can call directly; it's a side effect of the dispatch gate).

None of these appear in MCP `tools/list`. They are visible to
agents running inside primer's own session runtime.

For external MCP-hosted agents who need wait-for-event behaviour:
the right pattern is to **poll**. Call `trigger::get(id=...)` and
inspect `last_fired_at`, or `trigger::list_subscriptions(trigger_id=...)`
to observe subscription state. To check whether a SESSION is still
parked, call `workspaces::get_workspace_session(id=...)` and read its
status. Whether a session is waiting is not otherwise exposed as an
MCP tool; inspect it via the REST API or the workspace shell. Polling is ugly but works; yielding
is a primer-internal optimisation.

## Workflows

### Workflow 1 - internal agent waits for an external trigger to fire

**Goal.** An agent running inside a primer session needs to wait for a
nightly cron trigger before proceeding.

The agent's tool call:
```json
{
  "tool": "workspace_ext::subscribe_to_trigger",
  "arguments": {"trigger_id": "tr-3f9a1c0b7d42"}
}
```

`trigger_id` is the trigger's `id` (`tr-` plus 12 hex digits, the `id` that
`trigger::create` returned), not its slug; a slug answers
`trigger_not_found_or_disabled`.

What happens:
1. The tool handler resolves `tr-3f9a1c0b7d42`, creates a parked
   subscription row, and returns
   `Yielded(event_key="trigger:sess-7c1e:tr-3f9a1c0b7d42",
   resume_metadata={"subscription_id": "sb-X", "trigger_id": "tr-3f9a1c0b7d42"})`.
2. The session parks. The worker lease is released. The worker moves
   on to other work.
3. Cron midnight rolls around. The trigger dispatcher fires the
   trigger, sees the parked subscription, publishes
   `subscription_matched("trigger:sess-7c1e:tr-3f9a1c0b7d42")` (the key the
   parked session stored, once per parked subscription), and the worker
   pool marks that session resumable.
4. A worker claims the resumable session, calls
   `subscribe_to_trigger.resume(metadata, fire_context)`. The resume
   hook deletes the parked subscription and returns
   `ToolResult(output="trigger fired at <ts>, payload=<...>")`.
5. The LLM sees that result on its next turn and continues.

### Workflow 2 - yield is cancelled mid-park

**Goal.** Trace what happens when an operator cancels a parked
session.

1. Operator clicks "Cancel session" in the console. The console POSTs
   `/v1/sessions/{id}/cancel`.
2. The cancel handler detects `parked_status="parked"`, marks the
   session resumable with a `YieldCancelled` event marker.
3. A worker claims it. The tool's `resume()` is called with the
   cancelled marker.
4. The yielding tool's hook handles the cancellation (e.g.
   `subscribe_to_trigger.resume` deletes its parked subscription row
   so it doesn't leak).
5. The hook returns
   `ToolResult(output="cancelled by operator", is_error=False)`. The
   session is marked ended; no further LLM turns run.

## Gotchas

- **Yielding tools are invisible from MCP.** External agents
  connecting over `/v1/mcp` never see `ask_user`, `subscribe_to_trigger`,
  or `_approval`. If your agent needs wait-for-event behaviour from
  outside primer, poll the relevant REST endpoint instead.
- **Agent definition is re-read on resume.** Edits to the agent's
  prompt, tool set, or response_format take effect when the session
  resumes. This is sometimes the feature ("hot-edit my agent while
  it's parked") and sometimes the bug ("the tool I expected is no
  longer in the toolset").
- **`parked_state` only carries LLM messages + agent state, not
  snapshots of external state.** If the tool's external state (a
  subscription row,
  a watched file) is mutated by another actor while parked, the
  resume handler sees the current state, not the parked-time state.
- **Timeouts are wall-clock, not turn-based.** A `Yielded(timeout=300)`
  parks for at most 300 wall-clock seconds; the LLM's notion of "five
  minutes" doesn't enter into it.
- **Cancelling a yield ≠ cancelling the session.** The yield-
  cancellation produces a synthetic `tool_cancelled` result and lets
  the agent continue. Session cancellation ends the agent run
  entirely. Same console button can trigger either depending on
  state.
- **The timeout sweeper runs every 30 seconds.** A timeout you set
  to 5 seconds will fire between 5 and 35 seconds later, not exactly
  at 5. Don't use yields as a high-precision timer.
- **Sessions subscribed to one trigger each resume on their own key.**
  A fire walks the trigger's parked subscriptions and publishes the
  result onto each parked session's own key (`trigger:{sid}:{trigger_id}`):
  N subscribed sessions resume, one publish each, and a fire for one
  session cannot wake another (the old shared `trigger:{trigger_id}` key
  could, and parks already in flight on it keep that key until they
  drain). Firing a trigger with N subscribers still produces N claim
  races.
- **The fire-and-forget channel post that delivers ask_user prompts
  doesn't block the park.** If Slack is slow, the session still
  parks immediately; the post lands when it lands. Conversely, a
  failed channel post does NOT prevent the park - the prompt is
  effectively lost from the user's perspective. Operators monitoring
  channel-forward failures should treat them as user-visible bugs.
- **A `yielded` record with `kind: "tool_wait"` is not a yielding
  tool.** Behind an off-by-default worker flag
  (`tool_calls_as_claims_enabled`), a batch of tool calls can park the
  session as a batch instead of running in-process. You can see it as a
  `yielded` record whose payload carries `outstanding_task_ids` and
  `notifying_task_ids`, and as a session that reads `parked`. There is
  nothing to respond to: no approve or reject endpoint applies, and the
  session is meant to wake by itself when its tool tasks finish. The
  feature is unfinished: on current builds nothing runs the queued tool
  calls, so by the code such a session sits parked until the
  hard-coded one-hour timeout and then ends failed. Only a binding
  switch closes such a park immediately; an operator cancel or pause
  only sets a flag that is read at that timeout wake, so it does not
  take effect sooner. If you meet one, report it to the operator
  instead of waiting on it.
- **`wait_for_event` returns the event with stored secrets masked.**
  A CRUD event's payload is the entity's stored row; before it reaches
  your transcript every secret-bearing field (MCP `headers` and `env`
  values, OAuth `client_secret`, provider `api_key`, channel tokens,
  any key that looks like a key, token, secret or password) is replaced
  with `•••redacted•••`, the same masking `GET /v1/events` applies.
  Map keys (header and env NAMES) stay readable. The key-name rule is
  deliberately broad: any field whose name ends in `_key`, `_token`,
  `_secret` (and similar) is masked even when it is not a secret, so
  `parked_event_key`, `event_key` or `idempotency_key` in a payload
  also come back as `•••redacted•••`. Your `fields` / `expr` filter
  still matches the unmasked values; only the envelope returned to you
  is masked, so read such a field through the entity's own tool. You cannot
  read a secret back through an event; ask the operator if a task
  needs one.

## Related

- [tool-approval](tool-approval.md) - uses yields internally to park
  on `_approval` until the operator decides.
- [triggers-and-subscriptions](triggers-and-subscriptions.md) - fires
  the events that wake up `subscribe_to_trigger` parks.
- [channels](channels.md) - receives operator/user replies that wake
  up `ask_user` and `_approval` parks.
- [sessions](sessions.md) - owns the parked-status state fields.
  different storage column.
