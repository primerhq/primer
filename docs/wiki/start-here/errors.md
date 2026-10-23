---
title: Errors
summary: "The error envelope and the common error types, with what to do next for each."
tools: [system__describe_tool, system__list_toolset_tools]
related: [start-here/calling-tools, start-here/roles-and-approvals]
---
## What it is
Every toolset answers a failure with the same envelope: an object with a `type` and a `message`, and the result's error flag set; common types include the ten below alongside domain-specific ones such as `storage-error`. Each names a class of failure, and the class decides the fix: read the `type`, do the listed thing, and retry only after the fix.

## How to
Common error types, and the next move for each:

- `validation-error` - the arguments do not match the tool's schema (a missing required key, a wrong shape). Read the tool's schema (its reference page, or `system__describe_tool`) and send the arguments it names.
- `not-found` - the toolset, the tool, or the named entity does not exist. List the toolset's tools with `system__list_toolset_tools`, or search the wiki for the right name; do not retry the same id.
- `forbidden` - the run's principal ranks below the tool's role. It is a role refusal, not an approval: run the call from a higher-role run, or pick a tool whose floor the run clears.
- `approval-required` - the tool is gated and the call has nowhere to park for an approval (no session or chat). Invoke it through an agent session or chat, where the operator can decide the gate.
- `provider-error` - the toolset's provider failed to answer (an MCP server down, an OAuth token stale). The arguments were fine; check the toolset's connection and retry.
- `tool-call-error` - the inner tool raised while it ran. The `message` names the failure; fix what it names, then retry.
- `conflict` - the state moved under the call (a version it read was replaced, a guard it needed is gone). Re-read the current state and retry against it.
- `bad-request` - the request's shape is wrong: a missing piece the surface needs (a live session id for a yielding tool, a workspace root for a file path). Send the shape the tool expects.
- `unsupported-surface` - the tool was called where it cannot run (a session message tool outside a workspace session). Call it from the surface it needs.
- `tool-error` - a failure with no more specific type. The `message` is the only detail: read it, and treat a repeat as a defect to report.

## Gotchas
- `forbidden` and `approval-required` look alike and are not: one is a role the run cannot clear, the other is a gate that needs a place to park.
- On failure the envelope is the whole answer: there is no second payload field and no stack trace; the `message` is the detail.
- A success that carries bad news is not an error: a tool can answer with the error flag unset and still say the thing did not happen. Read the payload, not only the flag.

## Related
[calling tools](start-here/calling-tools), [roles and approvals](start-here/roles-and-approvals).
