---
title: "Tool grants and the rank rule"
summary: "What the tools list means, how scoped ids resolve, what a workspace session adds, and when one session may drive another."
order: 4
related: [agents/create-and-edit, start-here/roles-and-approvals, agents/invoke-an-agent]
---
## What it is

The agent's `tools` list is the model's whole tool surface: each entry is a scoped id, and the runtime exposes exactly the listed tools, never a whole toolset. Two things sit on top of the list: what the session composes onto the run (the workspace file tools, the per-turn console grant, external tools), and the drive rule, which decides when a run, a session or a person may drive another session at all.

## Concepts

### Scoped ids

A tool is granted as `<toolset_id>__<tool_name>` (for example `system__list_agents`). The toolset is either a built-in one (`system`, `workspaces`, `workspace_ext`, `workspace`, `misc`, `web`, `harness`, `trigger`, `collections`, `crud`, `canvas`) or a stored `Toolset` row (an MCP toolset, or the `search` row). A tool id with no `__` is checked as a toolset name but matches no tool, so it never provides one. The `canvas` toolset resolves while the canvas setting `agent_tools_enabled` is on, which it is by default now that the canvas panel ships; switched off, a `canvas` grant is refused like a toolset that does not exist.

### What the session adds

- **The workspace file tools.** In a session bound to a workspace, the file and shell tools (`ls`, `read`, `write`, `edit`, `glob`, `grep`, `exec`, scoped `workspace__ls` through `workspace__exec`) are always on and called directly by their scoped id. They are injected by the workspace binding, not picked from `tools`; you do not enumerate them.
- **The console grant.** `client__open_file` rides the turn the console grants it, outside the list.
- **External tools.** With `allow_external_tools` on, the tools the invoker provides from outside the session may be offered to the run; they bypass the allowlist by design, because the caller mediates every one.

### What a call gets

Routing tries the workspace tools first, then the toolset routing table:

- A call naming a tool of a toolset that could not list this turn is not dispatched; it comes back as a tool error (`toolset <id> is unavailable this turn: <reason>`).
- A call naming a tool the agent did not register is refused before dispatch with an unsupported-content error (422, `/errors/unsupported-content`) that ends the turn.
- The toolset's role floor is checked before any approval gate: a tool the run's role may not clear comes back as an in-band error result (the call is never executed), so the model can react.
- A tool handler that throws feeds its error text back to the model as the call's result. The turn continues; that is the recovery loop.

### Unavailable toolsets

An MCP toolset whose server is down, needs a sign-in or does not answer within 60 seconds no longer ends the turn. The turn starts without its tools, and the system message the model is sent ends with "Toolsets unavailable this turn, so their tools are not offered:" and one line per toolset saying why, in a fixed word (needs sign-in, unreachable, timed out or failed; never the server's own text). The notice is not stored in the history, and the next turn lists the toolset again, so it is back as soon as its server is. A Stop pressed during the wait ends the turn without waiting for the 60 seconds. A toolset that lists in-process is not covered: if one of those fails to list, the turn still fails.

### Yielding and approvals

A tool that yields (`system__ask_user`, `workspace_ext__sleep`, `workspace_ext__watch_files`, and `system__call_tool` when it wraps one) parks the turn: the agent holds no result until the answer or the deadline arrives, and a parked run releases its worker lease until it does. Yielding tools are not offered outside primer (over MCP). An approval-gated tool parks the turn under an approval record instead; to the agent the park is silent, because its next message contains the tool result or the rejection.

### The rank rule

Driving a session (steering it, restarting it, messaging it, cancelling it, resuming it, pausing it or Stopping it) is decided by access, not rank: the person behind the actor (a run's turn person, a person's REST call, the token's user over MCP) needs `full` access to the session's workspace. `read` alone is refused (`forbidden` from a tool, 403 `access_denied` over REST), and a workspace the person holds no access to answers as an unknown id. Who started the target session does not matter, and a creator who loses access to the workspace loses it to their own sessions too. See [Sharing and access](start-here/sharing-and-access).

- The turn a drive starts runs as the person whose input it is, not as the session's creator, so it gets that person's tools and access. A resume after a pause or an approval keeps the parked turn's person.
- Every turn still runs at its person's current role, capped by the role recorded for them. An unattributed session (a null creator) ranks as an ordinary user, never as the system.
- Rank remains for trigger fires: a `session_append` or `parked_session` fire into a session whose creator ranks above the fire's owners fails with `steer_outranks_fire`.

## Gotchas

- The grant is the whole surface. A tool the agent does not list cannot be called, even from a built-in toolset whose role the run clears: the refusal is a turn error, not a fed-back result.
- Two refusals look alike and are not. A role-floor denial is an in-band error result the model recovers from; a call on an unregistered tool ends the turn with an unsupported-content error.
- Listing `workspace__*` ids in `tools` does not grant the file tools where the session is not workspace-bound; they exist only under a workspace binding.
- A session that started as admin still records admin, but its runs are capped by the current role of the person each turn runs as.
- An unattributed session is user-rank, never system-rank: nothing about a null creator raises its rank.

## Related

[Create and edit agents](agents/create-and-edit), [Roles and approvals](start-here/roles-and-approvals), [Invoke another agent](agents/invoke-an-agent).
