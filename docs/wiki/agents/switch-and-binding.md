---
title: "Switch agents and the binding"
summary: "Change which agent drives a session with switch_to_agent and switch_binding, which apply at the end of the current turn."
order: 6
tools: [system__switch_to_agent, system__switch_binding]
related: [agents/invoke-an-agent, sessions/what-a-session-is]
---
## What it is

A session is bound to what runs its turns: an agent or a graph, under a model profile. The two tools change that binding. `system__switch_to_agent` hands the conversation to another agent with a handoff note, and `system__switch_binding` is the general form: an agent or a graph, an optional model profile, and a reason for the transcript. Both are queued: the current turn runs to the end, and the switch applies at the end of the turn.

## Concepts

- **The binding.** The session's binding names the agent (or graph) that executes its turns and the profile it runs under. The turn executor re-reads the bound definition on every turn, so what the new agent is does not have to be what it was at the moment of the switch.
- **Queued, not immediate.** A running turn owns its binding, so a switch asked for mid-turn cannot take effect at once. It is stored on the session row as the pending binding switch and applied at the next drain checkpoint, before any queued steer is realized, so a follow-up that was waiting behind the turn runs under the incoming binding. The call's result says so: "binding switch to '<target>' queued; applies at the end of this turn".
- **Verified before it is stored.** The target is checked at call time: an unknown agent or graph is a `not-found` error then, not a failed switch long after the agent that asked for it could react. A target that is deleted after the queue and before the apply degrades to a snapshot-less binding the executor resolves live.
- **The transcript keeps the handoff.** Applying the switch appends an agent marker to the session log: the from and to bindings, the actor, and the binding epoch. The history is not reset: the incoming agent inherits the session's history, summary and all.
- **The two tools.** `switch_to_agent` takes `{agent_id, prompt}` and is the agent-facing name: it queues a switch to that agent and carries the prompt as the handoff reason. `switch_binding` takes `{kind, agent_id | graph_id, profile_id?, reason?}`: it is the only way to hand a session to a graph, and the only way to carry a model profile across the switch. It is the only one of the two that names a graph.
- **A switch is not a message.** The handoff prompt is stored as the reason for the transcript; it is not delivered to the new agent as a new input. The new agent starts from the next input, with the inherited history as its brief.
- **Sessions only.** The tools act on the session the call runs in. With no calling session (an MCP call with no session context) the call is a bad-request: the switch is only available in workspace sessions.
- **The role floor still applies.** The tools are user-tier, so the run's principal must clear the floor, as with any tool call. And a person steering a session from REST is still bound by the rank rule. See [Tool grants and the rank rule](agents/tools-and-grants).

## How to

1. Decide what should run the rest of the session: an agent id or a graph id, and whether the profile should change with it.
2. For a plain agent handoff with an instruction, call `system__switch_to_agent` with the target id and the handoff prompt.
3. For a graph handoff, a profile change, or a reason of your own, call `system__switch_binding` with `kind`, the target id and, when needed, `profile_id` and `reason`.
4. Finish the current turn's work. The switch applies at the end of the turn; the next input runs under the new binding.

## Tool calls

### system__switch_to_agent

```json
{"toolset_id": "system", "tool_name": "switch_to_agent", "arguments": {"agent_id": "agent-coder", "prompt": "Implement the plan above."}}
```

Reference: [`system__switch_to_agent`](reference/tools/system/switch_to_agent).

### system__switch_binding

```json
{"toolset_id": "system", "tool_name": "switch_binding", "arguments": {"kind": "agent", "agent_id": "agent-coder", "reason": "implementation work from here"}}
```

Reference: [`system__switch_binding`](reference/tools/system/switch_binding).

## Gotchas

- The switch is queued, not immediate. The current turn still runs under the old binding, and the result says the switch applies at the end of this turn.
- If applying a queued switch times out or the row changed under the write, the switch stays queued for the next checkpoint; it is not dropped and not retried in the same instant.
- The handoff prompt is a reason, not a message. The incoming agent sees the history, not the prompt as an instruction to act on now.
- A graph handoff and a profile change are `switch_binding` only; `switch_to_agent` names agents.
- The switch does not start anything. The new binding runs from the next input; a session that receives none sits idle under the new agent.
- The marker is structural. It is neither a user input nor a terminal, so the session's pairing count is untouched and the switch shows in the log as the agent marker between the two bindings.

## Related

[Invoke another agent](agents/invoke-an-agent), [What a session is](sessions/what-a-session-is).
