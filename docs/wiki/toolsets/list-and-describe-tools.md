---
title: "List and describe tools"
summary: "Find a tool, read its schema and call it through call_tool."
order: 4
tools: [system__list_toolset_tools, system__describe_tool, system__call_tool]
related: [start-here/calling-tools, toolsets/builtin-toolsets, knowledge/the-platform-wiki]
---
## What it is
Three `system` tools take you from a tool's name to its result: `system__list_toolset_tools` lists a toolset's tools, `system__describe_tool` reads one tool's schema, role and live gate, and `system__call_tool` runs it. Every seeded agent holds `system__call_tool` directly; the other two are reached through it.

## Concepts
- `system__call_tool` takes a `toolset_id`, a `tool_name` and an `arguments` object (empty when you leave it out). It reaches any toolset the platform resolves: the [built-in toolsets](toolsets/builtin-toolsets) and the stored [MCP](toolsets/mcp-toolsets) and [Python](toolsets/python-toolsets) toolsets.
- `tool_name` is the tool's wire id inside its toolset: `create_agent`, not `crud__create_agent`; for the harness tools, `list`, not `harness__list`.
- The checks run in this order:
  1. The toolset must exist, else `not-found`.
  2. The toolset must list the tool, else `not-found`, naming up to 30 of its tools.
  3. Your run's rank must clear the inner tool's role, else `forbidden`.
  4. The approval gate: a gated tool parks your turn until a person decides.
  5. The tool runs.
- The inner tool gets your own context: your session, your workspace and your rank. There is no `principal` argument, and a model cannot pick the caller.
- The result passes through unchanged: the inner tool's output and error flag are the `call_tool` result.
- A pausing inner tool pauses your turn, and its answer arrives as the `call_tool` result.
- `system__describe_tool` answers one tool's document:
  - `scoped_id`, `description`, `args_schema` and `examples`;
  - `required_role`, `pauses_turn` and `needs_workspace`;
  - `approval`, the gate on the tool now (`gated`, and when gated its `type` and `policy_id`);
  - `call`: `via` is `call_tool` or `direct`, and `example` is the call to send;
  - `reference`, the path of the tool's wiki page.
- `system__describe_tool` also describes the `workspace` and `client` scopes. Those tools are not reachable through `system__call_tool`: call them directly, as the `call` field shows.
- `system__list_toolset_tools` lists every tool of one toolset with its id, description and schema. It asks the live provider, so an MCP toolset costs a round trip to its server.

## How to
1. Find the feature: `system__wiki_search` with your task in plain words, then read the page it names.
2. Read the tool's reference page, `reference/tools/<toolset>/<tool>`, for the exact call.
3. Before a write, check the live facts with `system__describe_tool`: the role it needs and the gate on it now.
4. Call it with `system__call_tool`, copying `call.example` and changing the values.
5. For a stored MCP or Python toolset there is no reference page: start from `system__list_toolset_tools`.

## Tool calls
### Describe one tool
`system__describe_tool` answers the schema, role, live approval gate and the call to send.

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "crud", "tool_name": "create_agent"}}
```

Reference: [`system__describe_tool`](reference/tools/system/describe_tool)

### List a toolset's tools
`system__list_toolset_tools` lists every tool a toolset offers now.

```json
{"toolset_id": "system", "tool_name": "list_toolset_tools", "arguments": {"toolset_id": "misc"}}
```

Reference: [`system__list_toolset_tools`](reference/tools/system/list_toolset_tools)

### Call a tool
`system__call_tool` runs the inner tool as your run and passes its result through. Here it is called directly, around an inner call.

```json
{"name": "system__call_tool", "arguments": {"toolset_id": "misc", "tool_name": "get_datetime", "arguments": {"timezone": "Asia/Dubai"}}}
```

Reference: [`system__call_tool`](reference/tools/system/call_tool)

## Gotchas
- `forbidden` is a role refusal, not a gate: no approval fixes it. Run the call from a session started at a higher rank, or ask a person who has the role.
- Describing a tool does not mean you may call it. Read `required_role` first.
- The reference page states the gate a fresh install ships. The operator can change gates, so the live answer is the `approval` field of `system__describe_tool`.
- A gated tool called where nothing can park (no session) answers `approval-required`.
- `system__list_toolset_tools` returns a whole toolset, and the `system` toolset holds more than a hundred tools. When you know the tool's name, `system__describe_tool` is far smaller.
- `system__list_toolset_tools` cannot list the `workspace` or `client` scopes (`not-found`). Describe their tools with `system__describe_tool`.
- An OAuth MCP toolset with no usable sign-in answers `sign-in-required` from `system__list_toolset_tools` and `system__call_tool`, and `provider-error` with the same message from `system__describe_tool`. Read `system__get_toolset_auth_status` and tell the user: only an admin can connect it.
- An MCP server that does not list its tools in time is a `provider-error`: 60 seconds for `system__call_tool`, 10 for `system__describe_tool`.
- Most built-in tools ignore an argument key they do not know. A wrong type is a `validation-error`.

## Related
[Calling tools](start-here/calling-tools), [built-in toolsets](toolsets/builtin-toolsets), [the platform wiki](knowledge/the-platform-wiki), [roles and approvals](start-here/roles-and-approvals).
