---
slug: mcp-server-reference
title: MCP server reference
section: reference
summary: How an external MCP client connects to Primer at /v1/mcp, lists tools, and calls them.
---

## Overview

Primer exposes a curated subset of its built-in tools to external MCP
clients at `/v1/mcp`. The endpoint speaks the MCP StreamableHTTP
transport. Operators control which tools are reachable via the
allowlist at `PUT /v1/mcp_exposure`.

## Transport

| Property | Value |
|---|---|
| Path | `/v1/mcp` |
| Protocol | MCP StreamableHTTP (stateful sessions, SSE-based) |
| Auth | Bearer token -- see Auth section below |

The endpoint does not speak stdio. External clients must connect over
HTTP to the running `primer api` process.

## Auth

Every request to `/v1/mcp` must carry a valid bearer token in the
`Authorization` header:

```code-tabs:bash
--- bash
Authorization: Bearer <token>
```

Anonymous requests receive `401` with `WWW-Authenticate: Bearer
realm="primer"`. Bearer tokens that lack the `mcp` scope receive
`403` with `{"code": "scope_required", "scope": "mcp"}`.

Cookie sessions (operator console) pass the scope check without
restriction. Mint MCP-specific tokens from the console with the `mcp`
scope; grant only the scopes the remote agent actually needs.

## Enabling exposure

Before any tools appear on `tools/list`, the operator must enable the
allowlist:

```code-tabs:bash
--- bash
curl -X PUT http://localhost:8000/v1/mcp_exposure \
  -H "Cookie: <session>" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "allowed_tools": ["system__call_tool", "misc__uuid_v4"]}'
```

`PUT /v1/mcp_exposure` is cookie-session-only; bearer tokens cannot
mutate the allowlist even with the `mcp` scope.

## Tool naming

Tools are identified by their scoped id: `<toolset_id>__<tool_id>`
(double underscore separator). Examples: `system__call_tool`,
`misc__uuid_v4`, `web__web-search`.

Only tools from the reserved built-in toolsets can be exposed. Tools
from user-defined toolset rows are always denied with reason
`not_system_toolset`. Built-in toolset ids are: `system`, `workspaces`,
`misc`, `web`, `harness`, `trigger`, `search`.

Additional constraints applied before a tool can be allowlisted:

- **Yielding tools** are denied (`yielding_unsupported`). MCP v1 has
  no park/resume primitive.
- **Workspace tools that require an active agent session** are denied
  (`needs_session`).

## Listing tools

Connect an MCP client to `/v1/mcp` and issue `tools/list`:

```code-tabs:bash
--- bash
# Using the MCP Inspector (example):
npx @modelcontextprotocol/inspector \
  --url http://localhost:8000/v1/mcp \
  --header "Authorization: Bearer <token>"
```

The response lists only the tools present in the active allowlist and
not blocked by the safety floor. If exposure is disabled (`enabled:
false`), the list is empty.

## Calling a tool

Issue a `tools/call` request with the scoped tool id:

```code-tabs:json
--- json
{
  "method": "tools/call",
  "params": {
    "name": "misc__uuid_v4",
    "arguments": {}
  }
}
```

## Result envelope

Every tool result has the same shape regardless of success or error:

```code-tabs:json
--- json
{
  "isError": false,
  "content": [
    { "type": "text", "text": "<tool output>" }
  ]
}
```

When `isError` is `true`, `content[0].text` carries the error message.
A tool that is not in the allowlist returns a JSON-RPC
`method-not-found` error rather than an `isError` result.

## Managing the allowlist

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/mcp_exposure` | Read the singleton row (enabled flag + allowlist) |
| `PUT` | `/v1/mcp_exposure` | Update enabled and/or allowed_tools (cookie session only) |
| `GET` | `/v1/mcp_exposure/available` | All catalogue tools with exposability verdict |

```ai-doc:mcp-exposure
```
