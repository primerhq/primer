---
title: Runbook conventions
summary: The authoring rule for a runbook; structure, the citation rule (scoped id + reference link), the gate table, and the index-row rule.
audience: admin
related: [purpose-map, reference, not-agent-callable]
---
A runbook is a step-by-step flow an agent can walk end to end: it names the exact scoped tool calls and the approval gates on the writes, so the agent never has to explore the raw REST API or probe the database to find the mechanism. This page is the authoring rule (W1-08b): every new runbook follows it, and the purpose-mapped index gets its row in the same PR.

## Structure
Every runbook has these sections, in this order.

### Goal
One line: what the flow accomplishes, in the user's words.

### Preconditions
The state the flow needs, each with the check that proves it:

- the state, and the scoped tool call (or REST GET) that reads it, and the expected answer
- what to do if the check fails (a pointer to the runbook or page that creates the missing state)

A precondition check is a READ: no runbook step may write before every precondition is verified.

### Roles and gates
- `runner`: `user` or `admin` (from [start-here/roles-and-approvals](start-here/roles-and-approvals); admin = platform config).
- `gated steps`: for every write step, the gate that SHIPS on the tool (the live gate, read with `system__describe_tool` at run time - the wiki states what a fresh install ships, an operator can retune it). A row per gated step: the scoped tool, shipped gate (`required` | `policy` | `none`), who approves, what the approval card shows. The gate table is mandatory on every runbook that writes, and the re-read in Verification proves the live gate was the one the table named.

### Steps
Numbered. Each step names the scoped tool with its reference link, a complete, validating JSON call body, and the expected result. A step that is gated says: the turn parks here until a person approves; do not retry the gated call. A step that no agent tool reaches (a REST-only or human-only surface) names it under [not-agent-callable](not-agent-callable) and states that it is the human step of the flow.

### Verification
The exact read call and the expected value, plus a behavioral check where one exists. A runbook is done when its Verification section reads the durable end state - not when a call answered 200.

### Refusal and failure
One row per error type the flow can produce: what it means, who may run the flow, and the next move (stop and report, check the named precondition, read the named page). `type=approval_required` is always "parked; a person decides; do not retry".

### Related
The concept page the runbook operationalizes, and the runbook that shares a precondition.

## The citation rule (W1-08b)
- Every step cites the scoped tool id (the toolset id, then `__`, then the tool name) and links the tool's reference page: `` [`system__describe_tool`](reference/tools/system/describe_tool) ``.
- The citation resolves on the live reference layer: the house hygiene suite (tests/docs) fails a page that names a tool the built-in toolsets do not have, or an endpoint the live OpenAPI schema does not carry. A stale citation is a red suite, not a runbook typo.
- The JSON body in a step validates against the live schema as documented on the reference page; no placeholder values.
- Capability knowledge lives in the wiki, never in the agent prompts: a runbook names the mechanism; the prompts only teach how to find this page.

## The index-row rule
Every runbook adds its row to the purpose-mapped index ([purpose-map](purpose-map), "The runbook set" table) in the same PR that lands the runbook. The index is a living page: a workstream that adds a workflow updates the index in the same PR (the DoD rule).

## Related
- [purpose-map](purpose-map): the index a runbook row lands in.
- [reference/](reference): the generated per-tool pages a citation links to.
- [not-agent-callable](not-agent-callable): the human steps a runbook names.
