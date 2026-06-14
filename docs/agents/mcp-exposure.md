---
slug: mcp-exposure
title: MCP exposure - primer as an MCP server
summary: How primer exposes its tools to external MCP clients via /v1/mcp, including the allowlist, scopes, and the safety gates that block dangerous tools.
related: [auth-and-tokens, tool-approval, semantic-search, yielding]
mcp_tools: []
---

# MCP exposure - primer as an MCP server

## Overview

Primer is two MCP citizens at once. It **consumes** MCP servers as a
client when an operator configures a `provider: mcp` toolset - those
remote tools then show up alongside primer's own. It also **hosts** an
MCP server at `/v1/mcp` (Streamable HTTP transport) so other clients
- Claude Desktop, Cursor, custom agents - can drive primer's own
capabilities the same way an agent inside primer would.

This doc is about the second half: the inbound MCP endpoint. The
endpoint is opt-in. A fresh install has `enabled=False` and an empty
allowlist, so the endpoint returns 503 until an operator turns it on
and explicitly chooses which tools to expose. The reason for that
opt-in posture is real: every exposed tool is a verb an unprivileged
remote agent can execute against your primer deployment, and a few
primer tools (the system `call_tool` meta-tool, the `web` HTTP
request tool, every yielding tool) are dangerous or technically
unsupported over MCP.

Once enabled, the surface area is exactly the intersection of three
sets: tools the operator has added to `allowed_tools`, tools that
pass the `is_exposable()` check at call time, and tools the
authenticated principal has scope for. Any agent connecting to
`/v1/mcp` sees only what falls in all three.

## Mental model

The endpoint is governed by a **singleton** `McpExposure` row in
storage. Its two fields are `enabled` (bool) and `allowed_tools`
(sorted, deduped list of scoped tool ids). The singleton is created
lazily on the first GET or PUT - a fresh install has no row, and the
default constructed at first touch is `enabled=False,
allowed_tools=[]`. That's the safest possible starting state: even if
something goes wrong with auth, no tool is reachable.

A **scoped tool id** uses double-colon delimiter: `<toolset_id>::<tool_id>`.
For example, `search::search_ai_docs`, `system::list_agents`,
`misc::get_datetime`. The double-colon is reserved - toolset ids and
tool ids can't contain it themselves.

Three filters run at every `tools/call`:

- **Allowlist.** The scoped id must appear in `allowed_tools`. Not in
  list → `tool_not_allowed`, 403.
- **Exposability.** The tool's provider is checked via
  `is_exposable(tool, provider)`. Today this rejects two classes:
  yielding tools (MCP v1 has no pause/resume primitive, so a tool that
  parks the session forever would deadlock the client), and workspace
  tools that need a live `AgentSession` (the MCP transport has no
  session context to bind them to). Future safety classes go through
  the same gate.
- **Auth scope.** The middleware that gates `/v1/mcp` requires the
  `mcp` scope. Bearer tokens minted without `mcp` get 401 on the
  handshake; cookie sessions bypass scope checks (operators trusted).

The `tools/list` MCP method emits exactly the tools that pass all
three. An agent calling `tools/list` and getting an empty array means
either MCP isn't enabled, the allowlist is empty, or the auth
principal's scope set excludes `mcp`. None of these are recoverable
client-side; the operator has to act.

Tool approval policies are **incompatible with MCP exposure** by
design. Approval pauses the call mid-flight waiting for an operator
(or LLM-judge / Rego) decision; MCP v1 has no mechanism for the client
to wait. So an allowlisted tool whose effective approval policy
resolves to `required` is **refused at `tools/call`**: the dispatch
path consults the same approval resolver the agent runtime uses, and
when the verdict is `required` it returns the tool as not exposed
(method-not-found on the wire) instead of running it. This is enforced
at call time, not at list time, so the tool may still appear in
`tools/list` but every call against it is blocked. The check is
re-evaluated on every call, so toggling or deleting the policy takes
effect immediately. To make such a tool actually callable over MCP,
the operator drops or disables the approval policy first.

## Lifecycle and states

The MCP exposure feature has no per-call state machine - every
request is independent. The singleton `McpExposure` row has two
relevant moments:

- **Fresh / safe.** No row exists, or `enabled=False` with empty
  allowlist. The `/v1/mcp` endpoint returns 503 with body
  `{"error": "mcp_disabled"}`. `tools/list` is not reachable.
- **Live.** `enabled=True` with at least one entry in allowlist. The
  endpoint serves the MCP handshake; `tools/list` returns the
  filtered tool set; `tools/call` dispatches.

Transitions are operator-driven via `PUT /v1/mcp_exposure`. The body
accepts `enabled`, `allowed_tools`, or both. When `allowed_tools` is
supplied, every scoped id is validated against the live catalogue -
unknown ids or non-exposable ids cause 422 with the offending id in
the response. This catches typos and prevents the operator from
allowlisting a tool that can never actually be called.

## MCP tools

This capability has no MCP tools of its own - exposure config is
operator-only (REST + console UI). Agents see exposure indirectly:
they observe the tool set that `tools/list` returns and accept that
this is the universe they can call. There's no "what would I be able
to call if I had more scope?" introspection endpoint.

## Workflows

### Workflow 1 - operator turns on MCP for the first time

**Goal.** Enable the endpoint, allowlist a starter set of tools, mint
a token, hand it to Claude Desktop.

1. Operator opens the MCP exposure page in the console. It shows
   "MCP exposure disabled". They click "Enable".
2. The page renders the live tool catalogue with checkboxes. Tools
   that pass `is_exposable()` are checkable; yielding and session-
   bound tools are greyed out with a tooltip explaining why.
3. Operator checks `search::search_ai_docs`, `search::search_tools`,
   `system::list_agents`, `system::get_agent`, `system::list_collections`,
   `system::get_document_content`, `misc::get_datetime`. They click
   Save.
4. The PUT validates each scoped id against the catalogue and writes
   the row. Subsequent `tools/list` calls return exactly those seven.
5. Operator goes to API Tokens, mints a token with `scope=["mcp"]`,
   pastes the plaintext into the desktop config.

### Workflow 2 - agent discovers it can't call a tool it expected

**Goal.** Diagnose a `tool_not_allowed` error from the agent side.

The agent sees `tools/call` return:

```json
{
  "is_error": true,
  "output": "{\"type\":\"tool-not-allowed\",\"message\":\"workspaces::create_workspace not in allowed_tools\"}"
}
```

What it means: this scoped id is not currently callable. The operator
either (a) hasn't added it to the allowlist, (b) had it but removed
it, or (c) the tool has an effective approval policy of `required`, so
it is refused at call time (approvals can't be collected over MCP).

The agent should:
1. Call `tools/list` to see what *is* available - there may be a
   functional substitute. `workspaces::create_workspace` not allowed but
   `workspaces::list_workspaces` is? Maybe the user wanted to use an
   existing one.
2. Surface the limitation clearly to the user: "I can read workspace
   metadata but the operator has not granted me workspace creation."
3. Not retry - the answer won't change without operator action.

## Gotchas

- **Hard-deny is empty in v1.** Earlier prototypes had a hardcoded
  block on `system::call_tool` and `web__http_request`. That's been
  removed - the operator owns exposure decisions. The constant
  `HARD_DENY` lives on as an empty `frozenset` for backward
  compatibility with importers. Tool dangerousness is a per-deployment
  judgment now.
- **Approval-gated tools are refused at call time, not hidden.**
  If the operator adds a tool to `allowed_tools` and *also* sets an
  enabled approval policy whose verdict is `required`, the tool may
  still appear in `tools/list`, but every `tools/call` against it is
  refused (returned as not exposed / method-not-found). The refusal is
  intentionally indistinguishable from a non-allowlisted tool on the
  wire so the allowlist's shape is not leaked; the real reason is in
  the audit log. From the agent's side, treat such a tool as
  uncallable and do not retry.
- **Yielding tools are also invisible.** The agent will not see
  `trigger::subscribe_to_trigger`, `misc::ask_user`, etc. Those
  primitives are unavailable outside of primer's own agent runtime.
- **The allowlist is a *set*, not a *prefix*.** There's no
  `system::*` wildcard. Each tool is listed by full scoped id. Tools
  added later via harness install need a follow-up allowlist update.
- **GZip is bypassed for `/v1/mcp`.** The endpoint streams chunked
  SSE events; the standard GZip middleware would buffer the whole
  response, breaking the streaming contract. A subclassed
  `GZipMiddleware` skips paths starting with `/v1/mcp`. If you write
  a new middleware that wraps the response, be aware of this carve-
  out.
- **One audit log line per tool call.** Every MCP `tools/call` writes
  a structured log entry with the principal, the scoped id, the
  outcome, and the duration. There's no separate audit table - the
  log stream is the audit trail.

## Related

- [auth-and-tokens](auth-and-tokens.md) - token minting and the `mcp`
  scope; required before any MCP call goes through.
- [tool-approval](tool-approval.md) - approval policies cause tools
  to silently disappear from MCP `tools/list`.
- [semantic-search](semantic-search.md) - `search::search_ai_docs` is
  the discovery tool for these very docs and is almost certainly
  going to be in your allowlist.
- [yielding](yielding.md) - why yielding tools can't be exposed over
  MCP and what the alternative is.
