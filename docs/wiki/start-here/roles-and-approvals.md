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
- Approval gates: a tool with an enabled approval policy parks the turn; the operator approves or rejects, and only that verdict moves the tool. A fresh install seeds unconditional gates on the `crud` construction tools and `python` toolset tools, and the operator can retune or add gates, so the live answer is the one `system__describe_tool` reports.
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
