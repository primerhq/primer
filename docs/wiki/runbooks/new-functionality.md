---
title: New functionality
summary: I want new functionality; web search for a free open-source API or MCP server first, the user check, the MCP integration, and the builder delegation last.
audience: admin
tools: [web__web_search, system__create_toolset, system__get_toolset_auth_status, system__update_agent, system__invoke_agent, crud__create_python_toolset, crud__update_python_toolset_source, crud__list_python_tools, system__describe_tool]
related: [purpose-map, toolsets/mcp-toolsets, toolsets/python-toolsets, runbooks/conventions]
---

## Goal
Give the platform a capability that does not exist yet, through the cheapest sufficient path: an existing free open-source API or MCP server, integrated, before any new code is written.

## Preconditions
- the requirement is stated: what the capability must do, in one paragraph.
  - check: none (the user states it; if it is ambiguous, `system__ask_user` before step 1).
- the operator's turn can reach the web and the construction tools.
  - check: `system__list_toolsets` ([reference](reference/tools/system/list_toolsets)) -> expected: the `web`, `system` and `crud` toolsets are listed.

## Roles and gates
- runner: `admin` (new toolset bodies that run code on the server host are admin-only).
- gated steps (the live gate at run time is read with `system__describe_tool`; the table is what a fresh install ships):
  - step 4 (MCP registration, host-code body only): `system__create_toolset` - shipped gate: policy (the Rego `HOST_CODE_TOOLSET_POLICY` gates python and stdio-MCP bodies; an http/sse server body passes with no gate); approver: admin only; card shows: the toolset body and provider.
  - step 6 (builder delegation, if reached): the crud writes inside the builder's turn (e.g. `crud__create_python_toolset`) - shipped gate: required; approver: admin only; card shows: the toolset id and the source body.

## Steps
### 1. Search the web for a free open-source API or MCP server
Call `web__web_search` ([reference](reference/tools/web/web_search)):

```json
{"toolset_id": "web", "tool_name": "web_search", "arguments": {"query": "free open-source <capability> API OR MCP server"}}
```

Expected: candidate names and links. `web__web_fetch` opens a candidate's page for the details (license, endpoint, auth model).

### 2. Check with the user that what was found meets the requirement
State the candidate plainly: what it is, what it costs (a free OSS endpoint), what it would take to wire it, and ask with `system__ask_user` whether it meets the requirement. The user decides; do not integrate on a guess.

### 3. Branch
- A candidate meets the requirement -> step 4 (integrate via the MCP path).
- Nothing sufficient exists -> step 5 (delegate to the builder).

### 4. Integrate via the MCP path (branch A)
Register the server as a toolset with `system__create_toolset` ([reference](reference/tools/system/create_toolset)); the provider is `mcp`, `sse` or `http` depending on the server's wire format (see [toolsets/mcp-toolsets](toolsets/mcp-toolsets)):

```json
{"toolset_id": "system", "tool_name": "create_toolset", "arguments": {"id": "<new-toolset-id>", "provider": "mcp", "config": {"<endpoint fields as the toolset/mcp-toolsets page documents>"}}}
```

Check sign-in state with `system__get_toolset_auth_status` ([reference](reference/tools/system/get_toolset_auth_status)) - a server that needs a sign-in is a human browser step (not agent-callable; see [not-agent-callable](not-agent-callable)), and the run stops there until the user has signed in. Where the MCP exposure allowlist must open the server, the REST surface is `PUT /v1/mcp_exposure` - a human step, named, never an exploration target.

Grant the new toolset's tools to the agent that needs them with `system__update_agent` ([reference](reference/tools/system/update_agent)), adding the scoped ids to the agent's `tools` list.

### 5. Delegate to the builder (branch B: nothing sufficient exists)
Call `system__invoke_agent` ([reference](reference/tools/system/invoke_agent)) with `agent_id` `builder` and a precise brief: the capability to implement, the toolset id to create, and the python source to write. The builder runs inline inside the operator's turn and returns what it built. Its construction path registers the python toolset with `crud__create_python_toolset` ([reference](reference/tools/crud/create_python_toolset)) (or `crud__update_python_toolset_source` to change the source after), and lists what it wrote with `crud__list_python_tools` ([reference](reference/tools/crud/list_python_tools)) - see [toolsets/python-toolsets](toolsets/python-toolsets).

### 6. Grant and verify the new capability
Add the new toolset's scoped ids to the consuming agent with `system__update_agent` (as in step 4), then verify the tool resolves:

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "<new-toolset-id>", "tool_name": "<a tool of it>"}}
```

## Verification
- `system__describe_tool` on a tool of the new toolset returns its schema, role and live gate - the tool resolves and the live gate matches the table above.
- `system__list_toolsets` lists the new toolset; the consuming agent's row (read with the tool that reads agent rows) carries the new scoped ids.
- a real call of the new capability returns a result (the behavioral check the requirement names).

## Refusal and failure
- type=forbidden (a host-code body for a non-admin) -> an admin runs the registration; stop and report.
- type=approval_required -> parked; a person decides; do not retry the gated call.
- type=validation-error (the toolset body) -> the named field is missing or wrong against the provider's schema; read the reference page for the provider shape and retry with the corrected body.
- type=not-found (the tool after the grant) -> the grant did not land: re-read the agent row and check the scoped id spelling (toolset id, double underscore, tool name).
- a sign-in is pending (the auth status is not signed in) -> the human browser step; stop, tell the user, and resume when they confirm.

## Related
- [purpose-map](purpose-map): the index row this runbook serves.
- [toolsets/mcp-toolsets](toolsets/mcp-toolsets): the MCP server shapes and the sign-in flow.
- [toolsets/python-toolsets](toolsets/python-toolsets): the python toolset construction.
