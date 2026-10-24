---
title: Roles and approvals
summary: "Who may run what: the three roles, the run's principal, the inner role check, and the approval gate."
tools: [system__call_tool]
related: [start-here/calling-tools, start-here/errors]
---
## What it is
Every agent run is a principal, and every tool names the role it needs. The role decides what the run may touch; the approval gate decides what it may touch at all. The two checks run in order, and only the first can fail fast.

## Concepts
- The three roles, ordered: `restricted` < `user` < `admin`. A tool's `required_role` is a floor: a run clears it when its principal ranks at or above it.
- The run's principal: the run is ranked as the one who initiated it; a call with no initiator ranks as an ordinary `user`. The principal is never an argument: the dispatch layer hands the inner tool the run's own context, so a model cannot pick the caller.
- The inner role check: a `system__call_tool` to a tool whose floor is above the run's principal is refused with `forbidden` before the approval gate, so a `user`-role run cannot reach an `admin` tool through the door.
- Approval gates: a tool with an enabled approval policy parks the turn; the operator approves or rejects, and only that verdict moves the tool. Every create, update and delete of an agent, a graph or a trigger is gated by default, whichever of these doors the call takes: the `crud` tools, the `system` and `trigger` tools that make the same writes (`system__update_agent`, `trigger__create`, the deletes), and `system__call_tool` to any of them, which meets the inner tool's gate. So are the writes that would walk around a gate or run code on the server host: creating, changing or deleting an approval policy (`system__create_tool_approval_policy` and its update and delete), creating or changing a Python or stdio MCP toolset (`system__create_toolset`, `system__update_toolset`; an http or sse MCP toolset is not gated), and a harness install, sync or uninstall (`harness__install`, `harness__sync`, `harness__uninstall`, reached through `system__call_tool`). A gate holds whatever the run's rank: a run an admin started parks like any other, and these gates (the policy, toolset and harness ones) are decided by an admin only, never from a messaging channel. A fresh install ships one gate per such tool (unconditional, except the toolset writes, whose gate is a rule on the body), an upgraded install gets the same gates, and the operator can retune or add gates, so the live answer is the one `system__describe_tool` reports. In a session a gated call parks for approval; over MCP, where nothing can park, it is refused unless the operator disables or retunes its gate. The gates bind the calls a model makes: a person acting directly over the REST API is not gated.
- A rejection answers the parked call with an error body that names the refusal, so the model can adapt instead of retrying blindly:

```json
{"rejected": true, "reason": "not now", "tool_name": "crud__create_agent", "arguments": {}}
```

## How to
1. Check the floor before you call: the tool's reference page and `system__describe_tool` both name the role and the live gate.
2. Expect the pause on a gated tool: the turn stops, the operator decides, the turn resumes with the verdict.
3. On a rejection, read the body: the `reason` is the operator's, and the `tool_name` and `arguments` are the call that was refused.

## Gotchas
- `forbidden` is a role refusal, not a gate: no approval prompt fixes a run that ranks below the tool's floor.
- A gated tool called where no one can approve (no session or chat to park on) answers `approval-required`; call it from an agent session or chat instead.
- The door's own role is `user`; what the check compares is the run's principal against the inner tool's floor, never the door's.

## Related
[calling tools](start-here/calling-tools), [errors](start-here/errors).
