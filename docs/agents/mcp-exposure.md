---
slug: mcp-exposure
title: MCP exposure - primer as an MCP server
summary: Use primer's fixed MCP discovery and dispatch surface, including exposure modes, caller authorization, and call-time safety checks.
related: [auth-and-tokens, tool-approval, semantic-search, yielding]
mcp_tools: []
---

# MCP exposure - primer as an MCP server

## Overview

Primer consumes external MCP servers as configured toolsets and also hosts an
inbound Streamable HTTP server at `/v1/mcp`. The inbound endpoint lets an MCP
client discover and call Primer's reserved built-in tools as the person behind
the client's cookie or bearer token.

The protocol advertises exactly three tools:

- `list_tools`: page and filter the caller-authorized catalogue.
- `find_tools`: search that catalogue by words, with optional semantic wiki
  ranking when the system collection has a working index.
- `call_tool`: call one discovered scoped id with its arguments.

The hundreds of target schemas are not advertised in `tools/list`. A client
requests schemas from `list_tools` or `find_tools` only when it needs them. This
keeps model context bounded while preserving the full authorized catalogue.

## Mental model

Discovery and dispatch are separate contracts, and neither trusts the
other:

- `tools/list` is a fixed advertisement (the three meta-tools), not a
  catalogue. The caller-visible catalogue comes from `list_tools` /
  `find_tools`, computed live against the caller's actor: exposure
  mode, role and toolset grants, and (in allowlist mode) the saved
  allowlist.
- `call_tool` rechecks the same floors plus the `mcp` scope and the
  live approval gate at call time. An id that was discoverable a
  moment ago can be refused the next second, and the refusal answers
  a fixed envelope that does not say why.
- What is hidden is not announced: an empty or narrowed catalogue
  never reveals the ids it withheld, and operator state (the mode,
  the saved allowlist) is managed over REST or the console, never
  through MCP.

## Exposure state

One `McpExposure` row controls the endpoint:

- `enabled` is the global switch.
- `mode` is `rbac` or `allowlist`.
- `allowed_tools` is the saved narrowing set used in `allowlist` mode.
- `updated_at` and `updated_by` are the audit stamp.

A new row defaults to `enabled=false`, `mode=rbac`, and an empty saved
allowlist. Rows created before the mode field existed keep their historical
allowlist meaning: migration 32 writes `mode=allowlist`, and a rolling reader
also interprets a missing mode that way.

The modes differ only in the operator narrowing step:

- `rbac`: every otherwise-authorized reserved tool is discoverable.
- `allowlist`: the tool must also be in `allowed_tools`.

Switching modes does not clear `allowed_tools`. The console leaves the saved
set visible but inactive in RBAC mode, so switching back restores the same
narrowing.

## Caller-aware catalogue

Discovery and dispatch apply the same floors:

1. The endpoint must be enabled.
2. In allowlist mode, the scoped id must be in the saved allowlist.
3. `is_exposable` must accept it. Yielding tools, tools that require an agent
   session, and tools of user-created toolsets are excluded.
4. A bearer token must carry the `mcp` scope. Cookie sessions carry their
   signed-in user's authority.
5. The caller's role must meet the tool's declared role floor.
6. `toolset_use` must allow the caller to use that reserved toolset and tool.

An MCP client acts as the token owner or signed-in user, never as the token
record itself. Built-in target handlers apply their normal entity access checks
too. A private row the person cannot read continues to answer like a missing
row.

User-created MCP, Python, or harness toolsets are deliberately not re-exported
through this endpoint. Registering an external MCP toolset makes it available
to authorized Primer runs; it does not turn Primer into a relay for that
server.

## Discovery tools

`list_tools` uses a deterministic scoped-id keyset cursor. Its fixed input
supports `cursor`, `limit`, `toolset_id`, `resource`, `read_only`,
`required_role`, `text_prefix`, and `include_schema`. Each item returns a stable
scoped id, reference path, purpose, role and capability facts, plus `schema`
only when requested.

Example:

```json
{
  "text_prefix": "collections_api__",
  "read_only": true,
  "limit": 20,
  "include_schema": true
}
```

`find_tools` requires a query and accepts `cursor`, `limit`, and
`include_schema`. Its lexical ranking always works, including on an install
without semantic-search providers or an indexed system collection. When
semantic wiki search is available, reference-page hits may improve the result,
but every hit is exact-intersected with the caller-authorized catalogue before
an id is returned. Search never reveals a hidden id.

Both tools return:

```json
{"items": [], "next_cursor": null}
```

The cursor is opaque to the client. Reuse it with the same filters; do not
parse or manufacture it.

## Calling a target

Call `call_tool` with the scoped id from discovery and the target arguments:

```json
{
  "scoped_id": "auth__auth_status",
  "arguments": {}
}
```

A successful result is:

```json
{"ok": true, "output": "{...target result...}"}
```

A hidden, removed, malformed, or newly unauthorized id gets the same bounded
answer:

```json
{"ok": false, "error": {"code": "tool_unavailable", "message": "Tool is unavailable."}}
```

This non-oracular error is intentional. It does not tell a caller whether the
id exists behind another role, allowlist, scope, or toolset grant.

`call_tool` does not trust a previous discovery response. It enters the
existing `invoke_exposed` path, which rechecks live exposure mode, allowlist,
exposability, MCP scope, role, toolset use, target schema, approval policy, and
existence immediately before dispatch. The existing invocation audit and
`mcp.tool_called` event record the outer call and target outcome.

An approval policy whose live verdict is `required` refuses the call. MCP v1
has no Primer-specific durable pause/resume exchange, so the endpoint does not
invent one. Yielding targets stay unavailable for the same reason.

## Operator workflow

1. Open System > MCP server.
2. Choose RBAC floor or Allowlist narrowing.
3. If using an allowlist, select the saved target set and press Save.
4. Enable the endpoint.
5. Mint a bearer token with the `mcp` scope and configure the MCP client for
   `/v1/mcp`.
6. In the client, inspect the three advertised meta-tools. Use `find_tools` for
   a goal, `list_tools` for deterministic browsing, then `call_tool` with a
   returned scoped id.

The console reports loading, empty and error states, keeps request buttons
focusable while busy, and uses success/error toasts. On a narrow viewport the
catalogue table scrolls horizontally rather than clipping its controls.

## Gotchas

- An empty discovery result can mean the endpoint is disabled, the token lacks
  `mcp`, no target passes the caller's role and toolset grants, or an allowlist
  narrows everything away. The result does not reveal which hidden ids exist.
- Approval changes after discovery take effect at the call.
- A schema change after discovery is validated at the call.
- There is no wildcard allowlist member. Each saved id is exact.
- GZip remains bypassed for `/v1/mcp` because the transport streams events.
- Generated REST tools use the same catalogue and access floors as curated
  tools. Their dispatch reaches the real FastAPI route through the trusted
  internal ASGI seam.
- Scoped ids and their reference paths are checked against the reviewed
  route-to-tool mappings. A declared mapping does not grant access: MCP
  still checks the live exposure, role, toolset grant and approval policy.

## Related

- [auth-and-tokens](auth-and-tokens.md)
- [tool-approval](tool-approval.md)
- [semantic-search](semantic-search.md)
- [yielding](yielding.md)
