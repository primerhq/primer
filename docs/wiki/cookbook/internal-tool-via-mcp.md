---
title: "Add an internal tool through MCP"
summary: "Register an MCP server as a toolset, check its tools and sign-in, and grant a tool to an agent."
order: 15
tools: [system__create_toolset, system__get_toolset, system__list_toolset_tools]
related: [toolsets/mcp-toolsets, agents/tools-and-grants]
---
## Goal

Bring an internal service's tools into this install. Register its MCP server as a toolset, check that it answers and whether it needs a sign-in, then give one of its tools to an agent. A person can also publish the tools to outside MCP clients.

## Prerequisites

- An MCP server the primer server can reach over `http` (streamable HTTP) or `sse`, here `https://kb.internal.example.com/mcp`.
- The header the server wants, if any, such as a bearer token. Header values are secrets: they are stored, and every read serves them masked.
- An `http` or `sse` server needs no admin, and its create is not gated. A `stdio` server launches a command on the primer host, so it needs an admin: a `stdio` body parks until an admin approves it, then succeeds only in a run an admin started (any other run gets `forbidden`). Over MCP, where nothing can park, it is refused. A python toolset is the same.

## Steps

### 1. Register the server

Store the toolset with `system__create_toolset`:

```json
{"toolset_id": "system", "tool_name": "create_toolset", "arguments": {"entity": {"id": "internal-kb", "provider": "mcp", "config": {"transport": "http", "config": {"url": "https://kb.internal.example.com/mcp", "headers": {"Authorization": "Bearer kb-token-REPLACE"}}}}}}
```

Response:

```json
{"id": "internal-kb", "provider": "mcp", "config": {"transport": "http", "config": {"url": "https://kb.internal.example.com/mcp", "headers": {"Authorization": "**********"}, "oauth": null}}, "harness_id": null}
```

The id is the toolset's name in every scoped id, so pick a short, stable one; the built-in names (`system`, `web`, `workspace` and the rest) are refused. The tool does not contact the server: unlike the console and `POST /v1/toolsets`, it runs no reachability check. Step 3 does.

### 2. Read the row back

Fetch the stored row with `system__get_toolset`:

```json
{"toolset_id": "system", "tool_name": "get_toolset", "arguments": {"id": "internal-kb"}}
```

Response:

```json
{"id": "internal-kb", "provider": "mcp", "config": {"transport": "http", "config": {"url": "https://kb.internal.example.com/mcp", "headers": {"Authorization": "**********"}, "oauth": null}}, "harness_id": null}
```

The row is the connection, not the tools: it holds no tool list. A set `oauth` block means the server signs in with OAuth.

### 3. List its tools

Ask the live server for its tools with `system__list_toolset_tools`:

```json
{"toolset_id": "system", "tool_name": "list_toolset_tools", "arguments": {"toolset_id": "internal-kb"}}
```

Response:

```json
{"tools": [{"id": "search_kb", "description": "Search the knowledge base.", "args_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}, {"id": "get_article", "description": "Read one article by id.", "args_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}]}
```

The answer tells you three things:

- A tool list: the server answers. Each `id` is a tool name; the scoped id is `internal-kb__search_kb`.
- `type=sign-in-required`: the toolset signs in with OAuth and nobody has connected it. A person connects it in the console ([MCP toolsets](toolsets/mcp-toolsets)); you cannot. `system__get_toolset_auth_status` reads the state.
- Any other error: the server did not answer. Check the URL and the header, and fix the row with `system__update_toolset`.

### 4. Give an agent the tool

Add the scoped id `internal-kb__search_kb` to the agent's `tools` list. `system__update_agent` replaces the whole agent: read it with `system__get_agent`, add the id, and send the full body back. The update is approval-gated by default, so it parks until a person approves it ([create and edit agents](agents/create-and-edit), [tool grants](agents/tools-and-grants)). An agent that holds `system__call_tool` reaches the tool without a grant: name `internal-kb` as the `toolset_id` and `search_kb` as the `tool_name`.

### 5. Publish the tools to outside MCP clients (a person)

No agent tool does this. An admin turns the MCP endpoint on and picks the published tools in the console's MCP server page (the exposure list, `PUT /v1/mcp_exposure`, takes only a console sign-in, never a bearer token). An outside client then sees them at `POST /v1/mcp` with a token that has the `mcp` scope ([not agent-callable](not-agent-callable)).

## Verify

`system__list_toolset_tools` answers the tools, a new session of the agent offers `internal-kb__search_kb`, and a call returns data from the internal service.

## Gotchas

- Saving the toolset proves nothing about the server. Always run step 3 before you grant a tool.
- If the server is down, needs a sign-in or does not answer within 60 seconds, the agent's turn starts without its tools, and its system message names the toolset as unavailable. The next turn tries again.
- The sign-in belongs to the toolset: one per toolset, shared by every agent that uses it, never one per person. Changing the URL or any OAuth setting of an OAuth toolset needs an admin and signs it out.
- To move the server to another host, send the header values again. A masked value sent back keeps the stored secret only on the same origin (scheme, host and port); on a new one the write answers `type=validation-error` and stores nothing.
- The server's tools carry no approval gate. To gate one that writes, an admin adds an approval policy for it ([approval policies](approvals/policies)).
- Outside MCP clients never see a pausing tool or a tool whose approval policy is `required`, even when it is on the exposure list.

## Related

- [MCP toolsets](toolsets/mcp-toolsets)
- [Tool grants and the rank rule](agents/tools-and-grants)
