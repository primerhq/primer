---
title: "MCP exposure and external clients"
summary: "Discover and call exposed targets through the three caller-aware MCP meta-tools."
order: 8
related: [start-here/accounts-and-tokens, toolsets/mcp-toolsets, approvals/policies]
---
## What it is

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

## Sessions

The endpoint is a stateful Streamable HTTP server: `initialize` returns an
`Mcp-Session-Id` header, and follow-up requests send it back. Two limits:

- The session lives in the API process that opened it. With more than one
  API replica the load balancer must keep one client's MCP requests on the
  same replica (session affinity); a follow-up request that reaches another
  replica answers 404 "Session not found".
- A session is bound to the credential that opened it (the cookie's user
  for a cookie session, the token row for a bearer token). A request that
  carries another credential's `Mcp-Session-Id` is answered exactly as if
  the session did not exist; the owner's own requests keep working.

## Concepts

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
7. A tool whose effective approval policy is the static `required` type is
   withheld, not advertised: it refuses every call, so a listed id could
   only ever answer `tool_unavailable`. Policies decided by an LLM judge or
   a Rego policy are NOT evaluated at discovery -- a listing must never
   trigger a model call -- so their tools stay listed and `call_tool`
   decides per call exactly as before.

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
id exists behind another role, allowlist, scope, or toolset grant. A target
schema that is itself malformed, whose patterns use their matching budget
(a pattern with overlapping or nested repeats, such as `(a|aa)+`, backtracks
on a long value), or whose validation runs past its time budget (a
`uniqueItems` over many objects, or a schema that branches at every level,
is slow) answers this same error: the schema belongs to the tool, not the
caller, and the validation runs bounded and off the event loop, so no call
can stall the API process while it validates. The arguments are validated up
to the app's request-body cap (the same bound the body-cap middleware admits
MCP posts to), not a smaller fixed size, so a body the server accepted is
never refused here.

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

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`external_tools__list_external_tool_calls`](reference/tools/external_tools/list_external_tool_calls) | Global external tool call list (audit + orchestrator poll). | user |
| [`external_tools__session_pending`](reference/tools/external_tools/session_pending) | Pending external tool calls for one session. | user |
| [`mcp__available_tools`](reference/tools/mcp/available_tools) | available_tools_endpoint. | admin |
| [`mcp__get_exposure`](reference/tools/mcp/get_exposure) | get_exposure_endpoint. | user |
| [`mcp__update_exposure`](reference/tools/mcp/update_exposure) | update_exposure_endpoint. | admin |
| [`tools__list_all_tools`](reference/tools/tools/list_all_tools) | List every tool currently exposed by every registered toolset (built-in + user-defined). | user |
| [`tools__list_tools`](reference/tools/tools/list_tools) | Flat catalogue of every tool exposed by every reachable toolset. | user |

## Related

- [auth-and-tokens](start-here/accounts-and-tokens)
- [tool-approval](approvals/policies)
- [semantic-search](knowledge/the-platform-wiki)
- [yielding](sessions/control-a-session)
