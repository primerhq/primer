---
title: "Approval policies"
summary: "Admin: gate a tool behind a person, a rule or a judge model. How gates pause a turn and how a decision is made."
audience: admin
order: 1
tools: [system__list_tool_approval_policies, system__get_tool_approval_policy, system__create_tool_approval_policy, system__update_tool_approval_policy, system__delete_tool_approval_policy, system__find_tool_approval_policies]
related: [start-here/roles-and-approvals, sessions/control-a-session, channels/reply-binding]
---
## What it is
An approval policy gates one concrete tool before it runs. The row names a `toolset_id` and a `tool_name`, and the gate is evaluated on every call, before the tool dispatches. When the verdict requires approval, the turn parks on an approval card and a person decides. All six policy tools need an admin, and the writes they make are themselves gated.

## Concepts
- **What a policy names.** A `toolset_id` and a `tool_name`, one concrete tool: no wildcards and no "every tool in this toolset". The toolset may be a built-in id or a stored Toolset row's id. A toolset has one id whatever a call spells it: the retired spellings (like `_system`) name the same toolset, so a policy written for the old spelling gates the tool under the new one, and a second policy for the same tool under the other spelling is refused when it is saved. A policy on the `external` toolset is refused: the caller supplies and answers those tools itself, so they are never gated.
- **The three kinds of gate**, the values of `approval.type`:
  - `required`: the gate trips unconditionally; a person decides every call.
  - `policy`: a Rego `policy` is evaluated against the call context (`tool_name`, `toolset_id`, `arguments`, `agent_id`, `session_id`, `requested_at`, and the channel the call came from). It must evaluate to a boolean `required` key (and an optional `reason`). When it says the call is allowed, the call runs with no park; when it says the call needs approval, the turn parks.
  - `llm`: a judge model. `provider_id` names a stored LLM provider, `model` names a model that provider publishes (checked when the policy is saved), and `prompt` is the judge's system prompt; the call context is appended as the user message.
- **`enabled`.** A policy with `enabled: false` is stored but skipped at evaluation. To switch a shipped gate off, disable it; deleting it brings it back at the next boot, because boot and Re-run seed re-create the missing shipped gates.
- **`timeout_seconds`.** An optional per-policy timeout. `null` falls back to the global yield cap. A timeout ends the gate as a rejection with a canned reason.
- **`approvers`.** Who may decide a gated call: `kind` is `anyone` (the default), `roles`, or `users`. An admin is admitted whatever the kind. A `policy` or `llm` verdict may name approvers for one call, narrowing who decides but never widening it: the decider must satisfy both, and the effective spec is stamped on the park. A restricted gate cannot be answered from a messaging channel, because a platform user is not an identified primer user: it is decided in the console.
- **`preview_args`.** Dotted paths into the tool's arguments that the approval card may show; a path allows its whole subtree. Every argument not named is drawn as its name and `<hidden>`, and the decision still acts on the real arguments. The policy's list overrides the tool's own declaration; with neither, only closed-set arguments (a boolean, a number, an enum) are shown. Each path must name an argument of the gated tool, and the write is refused when it does not.
- **How the gate is found.** The policy is looked up on every call (cached in-process for 30 seconds), so a change applies to the next call without a restart. Any error in a gate fails closed as `required` with a diagnostic reason: a Rego rule that cannot be evaluated, a judge that times out, a row that cannot be read.
- **The park and the decision.** A gated call parks the turn; the parked tool is named `_approval`. Approve runs the tool, and its result is the agent's next answer. Reject, timeout, or cancel end the gate as a rejection, and the agent reads a rejection result that carries the reason. A message to the parked session does not decide the gate: it is queued as the next turn.
- **Who may decide.** The console Inbox, or a reply in a bound channel when the gate has no approver restriction. Under the approver spec is the workspace's access: a person may decide a gate only when they hold `full` access to the session's workspace. `read` alone is refused with 403 `access_denied`, and a person with no access gets the session's 404. Admins hold every level. Answering does not change who the turn runs as: the parked turn resumes as the person it parked with.
- **One gate per operation, whatever the door.** A policy gates the tool itself, so the same call meets it through every door: the `crud` tools, the `system` and `trigger` tools that make the same writes, `system__call_tool` (a policy on the inner tool is honoured through it), and the MCP endpoint, where a call the gate would park is refused as `approval_required` because nothing can park there. A person acting over the REST API is not gated.
- **What a fresh install gates.** One `required` policy for each of the nine `crud` tools (ids `tool-approval-policy-crud-<tool-name>`) and their twins, plus the admin-tier gates: the three policy writes, `system__create_toolset` and `system__update_toolset` when the body is a Python or stdio MCP toolset (a Rego gate that lets exactly an http or sse MCP body, or an internal body, through), and the harness install, sync and uninstall. Those admin-tier gates are decided by an admin only, so a model cannot remove or relax the gate that protects a write without a person deciding it. An approval binds what it was shown: a harness install or sync names the overrides it applies, and the operation is refused if an input it was approved for changes before it runs, so a new decision is needed. These rows are ordinary storage rows: an operator may disable or retune any of them, and a tool that already has a policy keeps it and gets no second row.

## How to
1. Find the tool's scoped id and its reference page. `system__describe_tool` names its role floor and the live gate.
2. Write the policy with `system__create_tool_approval_policy`: the `entity` body carries `id`, `toolset_id`, `tool_name`, `approval`, and `enabled`, `timeout_seconds`, `approvers` and `preview_args` when you need them.
3. The next call to the tool meets the gate. Watch the console Inbox, or the bound channel when the gate is open, and decide there.
4. Retune with `system__update_tool_approval_policy`: it replaces the whole row, so read it with `system__get_tool_approval_policy`, change it, and send it all back. To switch a shipped gate off, send `enabled: false`.
5. Browse with `system__list_tool_approval_policies`, filter with `system__find_tool_approval_policies`, remove a row with `system__delete_tool_approval_policy`.

## Tool calls
### Create a required policy
`system__create_tool_approval_policy` stores the row and answers with it. This example gates `system__delete_agent` behind a person.

```json
{"toolset_id": "system", "tool_name": "create_tool_approval_policy", "arguments": {"entity": {"id": "tap-1", "toolset_id": "system", "tool_name": "delete_agent", "approval": {"type": "required"}}}}
```

Reference: [`system__create_tool_approval_policy`](reference/tools/system/create_tool_approval_policy)

### Create a policy rule for http_request
A `policy` row evaluates the Rego against the call and lets it run only when it answers `required` false. This one lets `web__http_request` through for one domain and parks everything else.

```json
{"toolset_id": "system", "tool_name": "create_tool_approval_policy", "arguments": {"entity": {"id": "tap-web", "toolset_id": "web", "tool_name": "http_request", "approval": {"type": "policy", "policy": "package primer.tool_approval\n\ndefault required := true\n\nrequired := false if {\n    startswith(input.arguments.url, \"https://api.example.com/\")\n}\n"}}}}
```

Reference: [`system__create_tool_approval_policy`](reference/tools/system/create_tool_approval_policy)

### Disable a policy
`system__update_tool_approval_policy` replaces the row. Sending the row back with `enabled: false` switches the gate off without deleting it.

```json
{"toolset_id": "system", "tool_name": "update_tool_approval_policy", "arguments": {"id": "tap-1", "entity": {"id": "tap-1", "toolset_id": "system", "tool_name": "delete_agent", "approval": {"type": "required"}, "enabled": false}}}
```

Reference: [`system__update_tool_approval_policy`](reference/tools/system/update_tool_approval_policy)

### List the policies
`system__list_tool_approval_policies` answers a page with `items`, `length`, `total` and `next_cursor`.

```json
{"toolset_id": "system", "tool_name": "list_tool_approval_policies", "arguments": {"offset": 0, "limit": 20}}
```

Reference: [`system__list_tool_approval_policies`](reference/tools/system/list_tool_approval_policies)

### Get one policy
`system__get_tool_approval_policy` reads the full row by `id`, for example before an update.

```json
{"toolset_id": "system", "tool_name": "get_tool_approval_policy", "arguments": {"id": "tap-1"}}
```

Reference: [`system__get_tool_approval_policy`](reference/tools/system/get_tool_approval_policy)

### Find the policies of one toolset
`system__find_tool_approval_policies` filters by a predicate tree; `predicate: null` matches every row.

```json
{"toolset_id": "system", "tool_name": "find_tool_approval_policies", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "toolset_id"}, "op": "=", "right": {"kind": "value", "value": "web"}}}}
```

Reference: [`system__find_tool_approval_policies`](reference/tools/system/find_tool_approval_policies)

### Delete a policy
`system__delete_tool_approval_policy` removes the row and answers the deletion ack.

```json
{"toolset_id": "system", "tool_name": "delete_tool_approval_policy", "arguments": {"id": "tap-1"}}
```

Reference: [`system__delete_tool_approval_policy`](reference/tools/system/delete_tool_approval_policy)

## Gotchas
- The six tools need an admin, and the three writes are gated by default: an admin's own run parks on a policy write, and the card is decided by an admin only.
- Preview paths are checked when the policy is saved: a path that names no argument of the gated tool is a refusal, because a typo would hide more than you meant, silently.
- A Rego rule that fails to compile is refused when the policy is saved, and any error at evaluation time fails closed as `required`, so a broken rule parks a call rather than letting it through.
- Gate the tool the agent calls, not `system__call_tool`: a policy on the inner tool is honoured through `call_tool`, while a policy on `call_tool` itself gates the door, not the inner call.
- One policy per `(toolset_id, tool_name)`: a second row for the same tool is refused with `type=conflict`. When two rows do exist (a raced create, or a row from before the check), the gate trips unconditionally and only an admin may decide it until the extra row is deleted.
- An update is a whole replacement, not a patch: read the row, change it, and send it all back.
- A timeout is a rejection, not a silent success: the agent reads a rejection result and continues.
- A new message to a parked session does not decide the gate; it waits behind the open turn.
- A decision names the gate it answers (`gate_id`). A respond that names a gate the session no longer holds answers 404 with code `gate_not_pending`, and one that names a gate replaced under the same `tool_call_id` answers 409 `approval_stale`; reload the pending list for both. A pending read never offers a gate whose decision is already stamped.
- A channel reply decides only a gate with no approver restriction; a restricted gate is answered in the console by an approver or an admin.
- Over `/v1/mcp` a call the gate would park is refused as `approval_required`: nothing can park there.
- An LLM judge must name a stored provider and a model that provider publishes; the write is refused when either is wrong.

## Related
[roles and approvals](start-here/roles-and-approvals), [control a session](sessions/control-a-session), [reply binding](channels/reply-binding).
