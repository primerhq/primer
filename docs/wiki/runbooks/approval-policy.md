---
title: Change the approval policy on a tool
summary: The approval gate on a tool; the gate type, the approvers and the timeout, read live before the change and verified with a re-read.
audience: admin
tools: [system__describe_tool, system__create_tool_approval_policy, system__update_tool_approval_policy, system__delete_tool_approval_policy, system__get_tool_approval_policy]
related: [purpose-map, start-here/roles-and-approvals, approvals/policies, runbooks/conventions]
---

## Goal
Make a tool's approval gate what the user asked for (gate it, relax it, retune who decides or the timeout), with the live gate read before and after - this is the highest-risk policy surface on the platform (a run that can relax a gate is the named threat of the admin gates), so the gate table and the re-read are mandatory.

## Preconditions
- the tool is named by its scoped id (the toolset id, then `__`, then the tool name) and the desired gate is stated: which type, who approves, and (where relevant) the Rego source or the LLM judge settings.
  - check: `system__describe_tool` ([reference](reference/tools/system/describe_tool)) on the tool -> expected: its schema, role and the LIVE approval gate. The live gate is the row an operator edits (or the shipped default when no row exists): it is NOT necessarily what the reference page says a fresh install ships.
- the tool exists and the caller may change its policy: the policy writes ship required-gated with admin-only approvers (Roles and gates below).

## Roles and gates
- runner: `admin`.
- gated steps (a fresh install ships; the live gate is read with `system__describe_tool` at run time):
  - every step 2 write: `system__create_tool_approval_policy` / `system__update_tool_approval_policy` / `system__delete_tool_approval_policy` - shipped gate: required; approver: admin only; card shows: the tool the policy gates, the gate type and the approvers the policy names.

## Steps
### 1. Read the LIVE gate
Call `system__describe_tool` ([reference](reference/tools/system/describe_tool)):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "<the tool's toolset>", "tool_name": "<the tool's name>"}}
```

Expected: the tool's live approval gate (type `required`, `policy` or `llm`, the approvers, the timeout) - or none. This is the before-state the verification diffs against.

### 2. Apply the exact policy write
The policy row is one per `(toolset_id, tool_name)`. Choose the write that matches the current state:

- no policy row exists (the tool runs on its shipped default) -> create:

```json
{"toolset_id": "system", "tool_name": "create_tool_approval_policy", "arguments": {"entity": {"id": "tap-delete-agent", "toolset_id": "system", "tool_name": "delete_agent", "enabled": true, "approval": {"type": "required"}}}}
```

- a policy row exists and the gate should change -> update (same body shape; only the fields that change):

```json
{"toolset_id": "system", "tool_name": "update_tool_approval_policy", "arguments": {"id": "tap-delete-agent", "entity": {"id": "tap-delete-agent", "toolset_id": "system", "tool_name": "delete_agent", "enabled": true, "approval": {"type": "policy", "policy": "package gates; required[\"required\"] := true"}}}}
```

- the gate should go back to the shipped default -> delete the row (`system__delete_tool_approval_policy` ([reference](reference/tools/system/delete_tool_approval_policy)), `{"toolset_id": "system", "tool_name": "delete_tool_approval_policy", "arguments": {"id": "tap-delete-agent"}}`).

The three gate types (`approval.type`):
- `required` - the gate trips on every call of the tool.
- `policy` - a Rego source (the `policy` field) is evaluated against the call context; it must return a document with a boolean `required` key (and an optional `reason`).
- `llm` - an LLM judge (a direct provider call: `provider_id`, the bare `model` name, and the judge `prompt`) answers the verdict.

The approvers (`approvers`) name who may decide a parked call: `{"kind": "anyone"}` (the default), `{"kind": "roles", "roles": ["<role>"]}` or `{"kind": "users", "users": ["<username>"]}`. Admins are always admitted regardless of the kind. A `timeout_seconds` names how long a parked call waits for a decision before it is refused.

### 3. A relaxation is stated as a relaxation
When the change RELAXES a gate (a tool that was gated goes ungated, or the approvers widen), say so plainly in the confirmation to the user: which tool, what the gate was (from step 1), what it becomes. A relaxation is the change a dictated flow must never make on a guess.

## Verification
- `system__describe_tool` on the tool again -> expected: the live gate exactly as applied (type, approvers, timeout), and it differs from the step-1 read in exactly the fields the change named.
- the policy row reads back: `system__get_tool_approval_policy` ([reference](reference/tools/system/get_tool_approval_policy)) on the row id -> expected: the body as applied (for a deletion: not-found).

## Refusal and failure
- type=approval_required -> the write itself is gated (it ships required, admin-only approvers): parked; a person decides; do not retry.
- type=forbidden -> a non-admin (or the approver set does not admit the caller): say who may run this and stop.
- type=validation-error (the Rego source) -> the source does not evaluate to a document with a boolean `required` key; the error names the problem. An LLM judge with an unknown `provider_id` or `model` is validated at create/update time and rejected here.
- type=conflict -> the row was edited between the read and the write; re-read the live gate (step 1) and re-apply against it.

## Related
- [start-here/roles-and-approvals](start-here/roles-and-approvals): the roles the approvers reference.
- [approvals/policies](approvals/policies): the mechanics of a parked call and its decision.
- [runbooks/access-policy](runbooks/access-policy): the other policy surface (who an entity is shared with).
