---
slug: mcp-server-reference
title: MCP server reference
section: reference
summary: The exposed-tool enumeration, argument schemas, result envelope, and auth requirements.
---

## Server discovery

| Method | Path | What it returns |
|---|---|---|
| GET | `/v1/mcp/server` | Server metadata (version, capabilities) |
| GET | `/v1/mcp/tools` | The full exposed-tool list |
| GET | `/v1/mcp/tools/{name}` | Schema for one tool |

The exposure list is built from the toolsets the operator has
marked as exposed via the MCP Server page.

## Tool catalogue

Each exposed tool surface as a separate row. The id is the
`<toolset>::<tool>` form.

| Tool id | What it does |
|---|---|
| `system::list_agents` | Read the agents list |
| `system::create_agent` | Create an agent |
| `system::list_sessions` | Read sessions |
| `system::create_session` | Start a session |
| `search::search_agents` | Semantic search over agent catalogue |
| `search::search_tools` | Semantic search over the tool registry |
| `search::search_ai_docs` | Semantic search over the AI doc set |

The full list is at runtime - the catalogue depends on which
toolsets are exposed in this instance.

## Argument schema shape

Every tool's argument schema is JSON-schema 2020-12. Example
for `system::create_agent`:

```code-tabs:json
--- json
{
  "name": "system::create_agent",
  "description": "Create a new agent.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "name": { "type": "string", "maxLength": 64 },
      "model": { "type": "string" },
      "toolsets": { "type": "array", "items": { "type": "string" } },
      "system_prompt": { "type": "string" }
    },
    "required": ["name", "model", "toolsets"]
  }
}
```

## Result envelope

Every tool result, whether success or error, has the same outer
shape:

```code-tabs:json,python
--- json
{
  "isError": false,
  "content": [
    { "type": "text", "text": "Agent created. id=ag-001" }
  ]
}
--- python
# Reading the envelope in a Python MCP client:
result = await client.call_tool("system::create_agent", args)
if result.isError:
    raise RuntimeError(result.content[0].text)
print(result.content[0].text)
```

## Auth

```callout:warning
Every MCP call carries the operator's bearer token. The
token's scope set gates the call; an unscoped tool call returns
403 even if the tool is exposed. Mint MCP-only tokens with
exactly the scopes the remote agent needs.
```

The token's scope set is checked at every call. Adding a scope
to a token requires reminting; primer does not edit token
scopes in place.

## Where to next

```ref:features/mcp-server
The feature page covers exposure toggles and the claude.ai
connector onboarding flow.
```
