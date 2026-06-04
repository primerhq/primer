---
slug: tool-approval
title: Tool approval
section: features
summary: Configure required, policy, and llm gates on specific tools; the approvals queue; per-tool overrides.
---

## The policy table

The Tool Approval page shows a policy table: one row per
`(toolset, tool)` pair that has a policy configured. Tools
without a row dispatch immediately. The empty initial state
means 'no policies; every tool dispatches'.

## Adding a policy

Pick a toolset, then a tool, then a kind:

| Kind | When to use |
|---|---|
| `required` | Operator visibility every time the tool is called |
| `policy` | Rego rule encodes the rule; no UI prompt |
| `llm` | Judge model decides; fast but probabilistic |

Save the policy. The next call to that tool routes through the
gate.

```code-tabs:python,curl
--- python
client.tool_approval.create(
    toolset_id="system",
    tool_name="delete_session",
    kind="required",
)
--- curl
curl -X POST https://primer.example/v1/tool_approval/policies \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"toolset_id":"system","tool_name":"delete_session","kind":"required"}'
```

## The approvals queue

A `required` gate parks the tool call. The prompt fires on the
agent's configured channel; if no channel is configured, it
lands on the IC bell in the console.

```mockup:channels-prompt
{ "platform": "slack", "question": "Approve delete_session call on sess-a1b2?", "options": ["Approve", "Reject"], "agentName": "ops-bot" }
```

Approve resolves the gate and the tool dispatches. Reject sends
a clean error back to the agent; the session continues but the
agent sees the deny and decides what to do next.

```callout:danger
Rejecting a privileged tool call is not a retry. The agent gets
an error, and if its prompt does not anticipate the deny it may
get stuck. Test the deny path in dev before turning on a policy
in production.
```

## Per-tool overrides

A toolset-binding can override per tool: deny a specific tool
inside an otherwise-bound toolset. The deny is enforced at
binding time (the agent cannot even ask to call it). Approval
policies enforce at dispatch time (the agent can ask but may be
blocked).

The two compose: binding decides what the agent can ask; policy
decides what dispatches.

## Where to next

```ref:concepts/tool-approval
The concept page covers the gate mechanic in detail, including
the policy and llm kinds the operator hits less often than
required.
```
