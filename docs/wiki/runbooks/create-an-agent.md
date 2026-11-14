---
title: Create or edit an agent
summary: Choose a usable profile, approve the full agent write, reread the owned definition and prove it answers.
audience: admin
tools: [system__create_agent, system__update_agent, system__delete_agent, system__get_agent, system__find_agents, system__list_agents, system__invoke_agent, system__list_model_profile_choices, system__describe_tool, crud__create_agent, crud__update_agent]
related: [agents/create-and-edit, agents/tools-and-grants, agents/invoke-an-agent, runbooks/create-and-run-a-graph]
---
## Goal

Create or edit an agent with a usable model profile, explicit tool grants and bounded instructions, then invoke it and verify the answer.

## Preconditions

- Run as a known authenticated user. A creator is stamped as the owner in `access.owners`; reading or invoking an existing agent needs read, editing needs full, and deletion needs owner. An administrator can also act within the platform's access rules.
- Read [`system__list_model_profile_choices`](reference/tools/system/list_model_profile_choices) and select a profile you can use. The worked calls below use `team-chat` only if that id is returned; otherwise carry the returned id into `model.profile_id`. An empty list is a stop: ask an admin to follow [configure a provider](runbooks/configure-provider), rather than creating provider configuration as a user.

```json
{"toolset_id": "system", "tool_name": "list_model_profile_choices", "arguments": {"limit": 200}}
```

- Check that `w7-agent` is absent before creating, or read the existing row before an edit. [`system__find_agents`](reference/tools/system/find_agents) filters the visible inventory by id; [`system__list_agents`](reference/tools/system/list_agents) is the browse alternative. Page through the returned inventory rather than treating one page as complete.

```json
{"toolset_id": "system", "tool_name": "find_agents", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "w7-agent"}}, "limit": 20}}
```

```json
{"toolset_id": "system", "tool_name": "list_agents", "arguments": {"limit": 20}}
```

- Decide the prompt, caps and exact scoped tool grants. The example grants only `system__wiki_read`, not the whole toolset. A grant does not confer access to unreadable toolsets or unusable profiles. Check each proposed tool with [`system__describe_tool`](reference/tools/system/describe_tool); use [tools and grants](agents/tools-and-grants) to select the narrow surface.

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "wiki_read"}}
```

## Roles and gates

`runner`: user. The shipped gate is the default policy, not a guarantee about this install. Read every relevant live gate before writing. If a gate parks the turn, a person allowed by that gate decides; do not retry, switch to the twin tool, edit a policy, or otherwise work around it. MCP cannot conduct a parked worker continuation: perform gated authoring inside a workspace session; a human answers the approval card in the console or the bound channel.

| write | shipped gate | who approves | default card preview |
|---|---|---|---|
| `system__create_agent` | required | a person allowed by the live approver rule | id, model, caps, tools and execution flags; update also names the target id |
| `system__update_agent` | required | a person allowed by the live approver rule | id, model, caps, tools and execution flags; update also names the target id |
| `system__delete_agent` | required | a person allowed by the live approver rule | the target id |
| `crud__create_agent` | required | a person allowed by the live approver rule | id, model, caps, tools and execution flags; update also names the target id |
| `crud__update_agent` | required | a person allowed by the live approver rule | id, model, caps, tools and execution flags; update also names the target id |

The default preview withholds prompt text and description. A permitted approver can inspect the complete pending call before deciding. All other tools on this page ship ungated.

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`system__create_agent`](reference/tools/system/create_agent):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "create_agent"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`system__update_agent`](reference/tools/system/update_agent):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "update_agent"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`system__delete_agent`](reference/tools/system/delete_agent):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "delete_agent"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`crud__create_agent`](reference/tools/crud/create_agent):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "crud", "tool_name": "create_agent"}}
```

[`system__describe_tool`](reference/tools/system/describe_tool) reads the live gate for [`crud__update_agent`](reference/tools/crud/update_agent):

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "crud", "tool_name": "update_agent"}}
```

## Steps

1. Create the definition through [`system__create_agent`](reference/tools/system/create_agent). The turn parks until a person approves; do not retry. After approval, the stored row must carry your user id as owner. `harness_id` must be unset; managed rows are changed through their harness.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "w7-agent", "description": "Answer a bounded review task", "model": {"profile_id": "team-chat"}, "tools": ["system__wiki_read"], "system_prompt": ["Answer the supplied task concisely. Read the wiki before using platform capabilities."], "max_tool_turns": 8, "max_output_tokens": 1024}}}
```

2. Reread with [`system__get_agent`](reference/tools/system/get_agent) and compare the profile, tools, prompt and caps with the intended definition. A successful write alone does not prove the intended fields persisted.

```json
{"toolset_id": "system", "tool_name": "get_agent", "arguments": {"id": "w7-agent"}}
```

3. For an edit, start with that readback and preserve every unchanged field. [`system__update_agent`](reference/tools/system/update_agent) replaces the whole entity; omitting a field resets it to its default. Keep the id fixed and use the live gate read above. This complete worked body changes the description and output cap; the turn parks again until approved.

```json
{"toolset_id": "system", "tool_name": "update_agent", "arguments": {"id": "w7-agent", "entity": {"id": "w7-agent", "description": "Answer a bounded review task with a short conclusion", "model": {"profile_id": "team-chat"}, "tools": ["system__wiki_read"], "system_prompt": ["Answer the supplied task concisely. Read the wiki before using platform capabilities."], "max_tool_turns": 8, "max_output_tokens": 1536}}}
```

4. The builder alternatives are [`crud__create_agent`](reference/tools/crud/create_agent) and [`crud__update_agent`](reference/tools/crud/update_agent). They perform the same checked writes and ship the same required gate. Choose one surface; these are alternatives to steps 1 and 3, not extra writes or a fallback after a refusal.

```json
{"toolset_id": "crud", "tool_name": "create_agent", "arguments": {"entity": {"id": "w7-agent", "description": "Answer a bounded review task", "model": {"profile_id": "team-chat"}, "tools": ["system__wiki_read"], "system_prompt": ["Answer the supplied task concisely. Read the wiki before using platform capabilities."], "max_tool_turns": 8, "max_output_tokens": 1024}}}
```

```json
{"toolset_id": "crud", "tool_name": "update_agent", "arguments": {"id": "w7-agent", "entity": {"id": "w7-agent", "description": "Answer a bounded review task with a short conclusion", "model": {"profile_id": "team-chat"}, "tools": ["system__wiki_read"], "system_prompt": ["Answer the supplied task concisely. Read the wiki before using platform capabilities."], "max_tool_turns": 8, "max_output_tokens": 1536}}}
```

5. Invoke the stored agent with [`system__invoke_agent`](reference/tools/system/invoke_agent). It runs a stateless subagent on the supplied prompt, with its own grants and the caller's identity, and returns `output`. It does not create a new durable agent-bound session. Wait for the actual output; a failed call or empty provider failure is not acceptance.

```json
{"toolset_id": "system", "tool_name": "invoke_agent", "arguments": {"agent_id": "w7-agent", "prompt": "Give one concrete check for a safe release."}}
```

6. Only for an explicitly disposable definition, after removing graph, active-session and subscription references, use [`system__delete_agent`](reference/tools/system/delete_agent). Deletion is destructive and separately gated. Keep the agent if another workflow, including [the graph runbook](runbooks/create-and-run-a-graph), needs it.

```json
{"toolset_id": "system", "tool_name": "delete_agent", "arguments": {"id": "w7-agent"}}
```

## Verification

Reread [`system__get_agent`](reference/tools/system/get_agent) after approval of each create or update. Compare `model.profile_id`, `tools`, `system_prompt`, `max_tool_turns`, `max_output_tokens` and `access.owners`. Check the live gate again if the operator changes policy before a later write; a prior approval is not permission for a different write.

```json
{"toolset_id": "system", "tool_name": "get_agent", "arguments": {"id": "w7-agent"}}
```

The invocation must return a real answer for the supplied prompt. A subagent shares no conversation history; include the full brief in `prompt`. If the optional cleanup ran, [`system__find_agents`](reference/tools/system/find_agents) must return no matching visible row instead of attempting another get as a success check.

```json
{"toolset_id": "system", "tool_name": "find_agents", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "w7-agent"}}, "limit": 20}}
```

## Refusal and failure

| result | meaning and next move |
|---|---|
| approval_required, approval-required or a pending approval | a session parks and a person decides; MCP without a park surface refuses the gated call; do not retry or change tool spelling |
| not-found | the row or reference is absent or unreadable; reread the id and sharing, without probing private rows |
| forbidden | the caller lacks the role, access or profile/toolset use; ask an owner or administrator |
| validation-error | inspect the named field; verify profile use, nonblank description, id and scoped grants before a new intentional write |
| conflict | duplicate id, harness-managed row, or `in_use_by` references block the write; keep the existing row or remove intended references first |
| tool-turn-cap | the invoked agent hit its tool-round cap; `partial_output` is diagnostic, not a completed answer |
| provider-error or bad-request | provider/stream failure or invocation-depth limit; stop and report the named cause; do not label an empty output successful |

## Related

- [Create and edit definitions](agents/create-and-edit): full replacement and immutable id.
- [Tool grants](agents/tools-and-grants): scoped ids and access.
- [Invoke an agent](agents/invoke-an-agent): stateless calls and nested approvals.
- [Create and run a graph](runbooks/create-and-run-a-graph): use the verified agent as a graph node.
