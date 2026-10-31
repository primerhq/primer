---
title: "Errors"
summary: "The error envelope and every error type, with what to do next."
order: 4
related: [start-here/calling-tools, start-here/roles-and-approvals]
---
## What it is
Every toolset answers a failure with the same envelope: an object with a `type` and a `message`, and the result's error flag set. The types below are the platform's own; a tool can also answer with domain-specific ones such as `storage-error`. Each type names a class of failure, and the class decides the fix.

## Concepts
The error types, and the next move for each:

- `validation-error` - the arguments do not match the tool's schema (a missing required key, a wrong shape). Read the tool's schema (its reference page, or `system__describe_tool`) and send the arguments it names.
- `not-found` - the toolset, the tool, or the named entity does not exist. List the toolset's tools with `system__list_toolset_tools`, or search the wiki for the right name; do not retry the same id.
- `forbidden` - the run's principal ranks below the tool's role, or the person behind the run holds too low an access level on what the tool touches (for example `read` on a workspace where the tool needs `full`). It is a refusal, not an approval: run the call from a higher-role run, pick a tool whose floor the run clears, or ask an owner for more access.
- `approval-required` - the tool is gated and the call has nowhere to park for an approval (no session or chat). Invoke it through an agent session or chat, where the operator can decide the gate.
- `provider-error` - the toolset's provider failed to answer (an MCP server down, an OAuth token stale). The arguments were fine; check the toolset's connection and retry.
- `sign-in-required` - the MCP toolset signs in with OAuth and has no usable shared sign-in (never connected, signed out, its settings changed). Only an admin can connect it, from the console: read its state with `system__get_toolset_auth_status`, tell the user, and do not retry until it says `connected`.
- `unavailable` - a service the tool needs is not wired into the process that ran it (session tools with no scheduler, semantic search with no search registry, the OAuth service, a configuration the server creates at startup). Nothing about the arguments is wrong: use the alternative the `message` names, or tell an operator; retrying the same call in the same place does not help.
- `tool-call-error` - the inner tool raised while it ran. The `message` names the failure; fix what it names, then retry.
- `conflict` - the state moved under the call (a version it read was replaced, a guard it needed is gone). Re-read the current state and retry against it.
- `bad-request` - the request's shape is wrong: a missing piece the surface needs (a live session id for a yielding tool, a workspace root for a file path). Send the shape the tool expects.
- `unsupported-surface` - the tool was called where it cannot run (a session message tool outside a workspace session). Call it from the surface it needs.
- `hop-limit` - a chain of 8 peer messages: the message that started this turn is already at the hop limit from a person, a trigger or a channel, so one more peer message would exceed the chain. The messages are circling: end the work with an answer, or hand it to a person (`system__ask_user`); do not re-send the same peer message.
- `rate-limited` - the sender session has already sent 20 peer messages in the last 10 minutes. The arguments were fine; the pace is not. The window slides, so wait before re-sending, or move the work off the peer path (a trigger, a channel, a person).
- `tool-error` - a failure with no more specific type. The `message` is the only detail: read it, and treat a repeat as a defect to report.

## How to
1. Read the `type`, not only the `message`: the type names the class of failure, and the class decides the fix.
2. Do the fix the type's row names.
3. Retry only after the fix: a retry that changes nothing is the defect the type is naming.

## Gotchas
- `forbidden` and `approval-required` look alike and are not: one is a role the run cannot clear, the other is a gate that needs a place to park.
- On failure the envelope is the whole answer: there is no second payload field and no stack trace; the `message` is the detail.
- A success that carries bad news is not an error: a tool can answer with the error flag unset and still say the thing did not happen. Read the payload, not only the flag.

## Related
[calling tools](start-here/calling-tools), [roles and approvals](start-here/roles-and-approvals).
