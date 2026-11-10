---
slug: cookbook/internal-tool-via-mcp
title: Expose An Internal Tool Via MCP
summary: An operator configures the MCP exposure mode and allowlist; the external agent then discovers and calls the published built-in tools over MCP through the three meta-tools.
mcp_tools:
  - system::create_toolset
  - system::get_toolset
---

## Goal
Make a company-internal tool reachable by an external MCP client (Claude Desktop, Claude Code, or claude.ai). Two shapes:

- The tool is one of this install's built-in (or generated REST) tools. The operator publishes it through the MCP exposure settings, and the external client discovers it with the `list_tools` / `find_tools` meta-tools and calls it with `call_tool`.
- The tool lives in an external service behind an MCP endpoint. Register that service as a toolset so agents of this install can call it in-session (steps 1-2). Such a toolset's tools are NOT publishable to outside MCP clients: the exposure surface publishes only the reserved built-in and generated tools, never a user-registered toolset's.

## Prerequisites
- Primer is running and reachable, and you can authenticate to its MCP endpoint with a bearer token carrying the `mcp` scope.
- For the second shape: an internal service or MCP connector you want to surface (for example a company knowledge service behind an MCP-speaking endpoint).

```callout:info
MCP exposure is operator-configured, not agent-configured. There is no MCP tool that turns exposure on, changes the mode, or selects which tools are published. Those steps happen over REST or in the console. The MCP tools below cover only the part an agent or automation can drive: registering the connector toolset and confirming it exists. See `mcp-exposure` for the full exposure model.
```

## Steps
### 1. Operator: register the MCP-connector toolset
`system::create_toolset`
```json
{
  "entity": {
    "id": "internal-kb",
    "provider": "mcp",
    "config": {
      "transport": "http",
      "config": { "url": "https://internal-kb.example.com/mcp" }
    }
  }
}
```
Response:
```json
{ "id": "internal-kb" }
```
This registers an MCP-connector toolset whose tools come from the upstream `internal-kb` server. Agents of this install can then be granted its scoped ids (for example `internal-kb__search_kb`) and call them in-session.

```callout:info
This example registers an `http` connector, which any non-restricted user may do. A toolset on the `stdio` transport launches a command on the Primer host, so creating or changing one requires the admin role, and it is approval-gated by default (an http or sse toolset like this one is not gated). The gate runs first: in an admin's run, `system::create_toolset` or `system::update_toolset` with a stdio body parks until an admin approves it; a run started by a lower role parks too, and the approved call is then refused `type=forbidden`; over `/v1/mcp`, where nothing can park, the call answers `approval_required` while the gate is on, and `type=forbidden` once it is disabled (the caller's role is not known to the tool there). An admin registers a stdio toolset over REST (`POST /v1/toolsets`) or in the console. The same holds for a python toolset, whose source runs on the Primer host. Changing an existing OAuth toolset's `url` or any OAuth setting is admin-only, and an accepted change signs out its shared identity. The OAuth `resource_uri` and `token_endpoint` are origin-bound fields too. Re-enter header values and the OAuth client secret when moving an endpoint; even an admin who moves the host with a masked value left alone gets `type=validation-error` ("re-enter the secret") and stores nothing, as `system::update_llm_provider` and `system::update_embedding_provider` do for an `api_key`; a changed path keeps it. See [MCP toolset sign-in](../mcp-toolset-sign-in.md). `create_*` refuses a body that carries a masked value `get_*` served, so to copy a row send the real secrets.
```

### 2. Operator: confirm the toolset and its tools
`system::get_toolset`
```json
{ "id": "internal-kb" }
```
Response:
```json
{ "id": "internal-kb", "provider": "mcp", "tools": [ { "id": "search_kb" }, { "id": "get_article" } ] }
```
Note the tool ids (for example `search_kb`, `get_article`); the scoped ids (`internal-kb__search_kb`, `internal-kb__get_article`) are what an agent grant names.

### 3. Operator: configure the MCP exposure (REST or console)
This step has no MCP tool. The operator does it out of band, and it decides what the outside clients see:

- In the console, open **MCP Server**, pick the exposure mode on the endpoint panel, and enable the endpoint:
  - **RBAC floor**: every otherwise-authorized built-in tool is discoverable to a caller whose role and grants clear the floor.
  - **Allowlist narrowing**: the caller only sees tools in the saved allowlist. In the **Exposed tools** table tick the tools to publish (or use **Recommend safe defaults** for a read-only `get_*` / `list_*` / `find_*` set) and click **Save**.
- Or call the REST exposure endpoint directly: `PUT /v1/mcp_exposure` with `{"mode": "rbac"}` or `{"mode": "allowlist", "allowed_tools": [...]}`, plus `{"enabled": true}` to turn the endpoint on.

Saving replaces the saved allowlist atomically in allowlist mode; switching modes never clears it, so returning to allowlist mode restores the saved narrowing. The exposure surface publishes the reserved built-in and generated tools only: a user-registered toolset such as `internal-kb` is never in that table, and no allowlist entry can add one. See `mcp-exposure` for the endpoint and the mode semantics.

### 4. Operator: mint a scoped token (REST or console)
Also out of band: create an API token with the `mcp` scope (add `sessions:read` and similar only if the client truly needs them). Mint a separate token per client and store it securely; the token is shown once. This step has no MCP tool either.

### 5. Consumer: the external agent discovers the published tools
With the endpoint enabled and a bearer token configured, the external client's `tools/list` advertises exactly three meta-tools, never the target tools:

```json
{ "tools": [ { "name": "list_tools" }, { "name": "find_tools" }, { "name": "call_tool" } ] }
```
The client discovers the caller-visible catalogue through the meta-tools, for example:

`list_tools`
```json
{ "limit": 50 }
```
or `find_tools` for a purpose:
```json
{ "query": "search a collection" }
```
Response (abridged; `include_schema: true` adds the target's `args_schema`):
```json
{ "items": [ { "scoped_id": "collections__search", "purpose": "Search a collection. One tool for every rung: mode defaults to the best the collection offers and the result states mode_used." } ], "next_cursor": null }
```
Only tools the operator's mode and the caller's role, grants, and (in allowlist mode) the saved allowlist permit appear. Discovery is caller-aware: an id the caller cannot use is never revealed, and a returned page does not authorize a later call - the call rechecks live state.

### 6. Consumer: call a published tool
The agent calls a discovered tool by its scoped id through `call_tool`, for example:

`call_tool`
```json
{ "scoped_id": "collections__search", "arguments": { "collection": "kb", "query": "onboarding checklist" } }
```
Response (abridged):
```json
{ "ok": true, "output": "{ \"hits\": [...], \"mode_used\": \"fulltext\" }" }
```
Refusals answer a fixed envelope instead of the inner detail: an unavailable or unseen target answers `{"ok": false, "error": {"code": "tool_unavailable", "message": "Tool is unavailable."}}`, a role or scope denial answers `access_denied`, and an argument that does not match the target's schema answers `invalid_arguments`. The call runs through primer's normal dispatch, scoped by the bearer token.

## Verify
`tools/list` from the external client names exactly the three meta-tools; `list_tools` / `find_tools` return the operator-visible catalogue (and none of the hidden ids); and a `call_tool` to one of them returns data from the service. The console MCP Server page shows the mode, the saved-allowlist state, and the last-edited timestamp.

## Gotchas
- The exposure surface publishes built-in and generated tools only. Registering a connector toolset (step 1) makes its tools reachable to this install's agents, never to outside MCP clients.
- Exposure is mode-based. In allowlist mode the saved set is replace-on-save: deselecting a tool removes it from every client immediately; coordinate before pruning a published tool that clients depend on. In RBAC mode the saved set stays stored but inactive until you switch back.
- Scope tokens minimally. An `mcp`-scoped read-only assistant does not need write scopes; an over-scoped token is the most common source of unexpected agent behaviour.
- Discovery is not authorization. `call_tool` rechecks exposure mode, allowlist, role, grants, scope, and the approval gate at call time; an id that was listed a moment ago can be refused the next second.

## Related
- `mcp-exposure`, `toolsets`, `tool-approval`
- `cookbook/create-and-run-a-session`
