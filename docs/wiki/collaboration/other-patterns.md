---
title: "Other collaboration patterns"
summary: "The other ways agents coordinate: cross-session messaging, trigger and subscription events, and in-turn delegation to agents and graphs."
order: 2
tools: [workspaces__message_session, system__wait_for_event, trigger__create_subscription, trigger__fire_now, system__invoke_agent, workspace_ext__invoke_graph]
related: [collaboration/coordinator-pattern, sessions/messaging-other-sessions, triggers/subscriptions, agents/invoke-an-agent, triggers/waiting-for-events]
---
## What it is
Besides the file-based coordinator pattern, the platform composes four more coordination shapes from existing tools: a session messaging another session, a trigger and subscription that start work from an event, and in-turn delegation to another agent or a sub-graph. Each pattern below gives the shape, when to use it, the exact tools and the failure modes. The scope of this page is open: these are the patterns evidenced on today's surfaces, and the section grows with the survey, not with a fixed list.

## Concepts
- **Cross-session messaging.** One session sends another session a message with `workspaces__message_session`: an idle target starts its next turn with it, a busy target reads it at its next turn boundary. The sender can park its own turn on the `session.messaged` event with `system__wait_for_event` until the reply lands. This is the follow-up channel: use files for the payload and messages for the nudge.
- **Trigger and subscription events.** A trigger (schedule, webhook, channel or delayed) plus a subscription that says what a fire does (start a fresh agent or graph session with the fired payload) is the "do this later, or on this event" pattern: no session holds a watch at all, the claim engine times the fire, and the work starts where the subscription points. `trigger__fire_now` fires a trigger by hand, which is how you test the binding.
- **In-turn delegation.** `system__invoke_agent` runs another agent on a prompt inside the current turn and returns its text: the subagent runs inline, its identity is recorded, and a depth limit and a cap bound the fan-out. `workspace_ext__invoke_graph` runs a child graph inside the current workspace session and nests its state under this session, which is the shape for a multi-step workflow whose sequencing should live in the graph engine rather than the caller's context.
- **Shared knowledge surfaces.** The read-only platform wiki and a collection of documents are the stateless variant: agents coordinate by reading the same pages or documents instead of writing to each other's files. The wiki carries what exists (read it, never write to it); a collection the agents hold access to carries the shared reference material.

## How to
1. For a one-off follow-up to another session, message it and wait for the reply with the event wait.
2. For work that must start on an event or a schedule, create the subscription behind the trigger and test it with `fire_now`.
3. For a self-contained subtask inside your own turn, invoke the specialist agent; for a multi-step workflow, invoke the graph.
4. For shared reference material, point every participant at the same wiki page or collection document instead of copying it into each session.

## Tool calls
### Message another session
Starts the target's next turn, or queues behind its current one:

```json
{"toolset_id": "workspaces", "tool_name": "message_session", "arguments": {"session_id": "sess-7c21e04a9d13", "text": "The spec is in docs/api.md; pick up the client build when you are free."}}
```

Reference: [`workspaces__message_session`](reference/tools/workspaces/message_session)

### Wait for the reply
Parks your turn until a message reaches your own session:

```json
{"toolset_id": "system", "tool_name": "wait_for_event", "arguments": {"event_types": ["session.messaged"], "fields": [{"path": "session_id", "op": "eq", "value": "sess-3f9a1c8d2b7e"}], "timeout_seconds": 900}}
```

Reference: [`system__wait_for_event`](reference/tools/system/wait_for_event)

### Subscribe a fire to fresh work
A fire starts a fresh agent session in a named workspace with the fired payload:

```json
{"toolset_id": "trigger", "tool_name": "create_subscription", "arguments": {"trigger_id": "tr-3f9a1c0b7d42", "config": {"kind": "agent_fresh_session", "workspace_id": "ws-ops", "agent_id": "alert-summariser"}, "payload_template": "Summarise the overnight alerts. Fired at {{ fired_at }}.", "parallelism": "skip"}}
```

Reference: [`trigger__create_subscription`](reference/tools/trigger/create_subscription)

### Fire a trigger by hand
Tests the binding without waiting for the schedule or webhook:

```json
{"toolset_id": "trigger", "tool_name": "fire_now", "arguments": {"id": "tr-3f9a1c0b7d42"}}
```

Reference: [`trigger__fire_now`](reference/tools/trigger/fire_now)

### Invoke a sub-agent
Runs the specialist inline inside your turn and takes its text back:

```json
{"toolset_id": "system", "tool_name": "invoke_agent", "arguments": {"agent_id": "agent-researcher", "prompt": "Summarise the RFC."}}
```

Reference: [`system__invoke_agent`](reference/tools/system/invoke_agent)

### Invoke a sub-graph
Runs the child graph in this session; its state nests under yours:

```json
{"toolset_id": "workspace_ext", "tool_name": "invoke_graph", "arguments": {"graph_id": "incident-pipeline", "input": "Triage the overnight alerts."}}
```

Reference: [`workspace_ext__invoke_graph`](reference/tools/workspace_ext/invoke_graph)

## Gotchas
- Messaging has guards with exact answers: a hop limit (two agents answering each other stop at 8 hops from a person), a rate limit (20 messages per 10 minutes per session), and the rule that a message parked against `ask_user` or an approval is never the answer. Decide up front which side ends the exchange.
- A graph target never reads a message: each graph run takes its input only from the session's stored `graph_input`, so a message to a graph session restarts it, it does not feed it.
- A trigger fire into a session whose creator ranks above the fire's owners fails with `steer_outranks_fire`; check the subscription points at a session the fire may drive.
- A rejected or timed-out approval lands differently on the two delegation surfaces. In an invoked graph it fails the child graph: the answer to your call is an error result, not the child's output.
- In a sub-agent the same refusal is delivered as the error result of the sub-agent's parked call, and its turn may continue: the sub-agent reads the refusal and may still finish, so the outer `invoke_agent` can return its final text successfully.
- Delegation is synchronous with your turn: the invoked work runs before your turn continues, so keep the delegated work self-contained; for long, parallel or event-driven work use the file pattern or a subscription instead.

## Related
[Coordinator and sub-agents](collaboration/coordinator-pattern), [Messaging other sessions](sessions/messaging-other-sessions), [Subscriptions](triggers/subscriptions), [Invoke another agent](agents/invoke-an-agent), [Waiting for events](triggers/waiting-for-events).
