---
slug: cookbook/internal-tool-via-mcp
title: Expose An Internal Tool Via MCP
summary: An operator registers an MCP-connector toolset and configures the exposure allowlist; the external agent then discovers and calls the now-published tool over MCP.
mcp_tools:
  - system::create_toolset
  - system::get_toolset
---

## Goal
Make a company-internal tool reachable by an external MCP client (Claude Desktop, Claude Code, or claude.ai). An operator registers the toolset and decides which of its tools are published; the remote agent then sees the published tools through the standard MCP `tools/list` discovery and calls them in any conversation.

## Prerequisites
- Primer is running and reachable, and you can authenticate to its MCP endpoint with a bearer token carrying the `mcp` scope.
- An internal service or MCP connector you want to surface (for example a company knowledge service behind an MCP-speaking endpoint).

```callout:info
The MCP exposure allowlist is operator-configured, not agent-configured. There is no MCP tool that turns exposure on or selects which tools are published. Those steps happen over REST or in the console. The MCP tools below cover only the part an agent or automation can drive: registering the connector toolset and confirming it exists. See `mcp-exposure` for the full exposure model.
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
This registers an MCP-connector toolset whose tools come from the upstream `internal-kb` server. Registering the toolset does not publish it; its tools are not yet in any external client's palette.

### 2. Operator: confirm the toolset and its tools
`system::get_toolset`
```json
{ "id": "internal-kb" }
```
Response:
```json
{ "id": "internal-kb", "provider": "mcp", "tools": [ { "id": "search_kb" }, { "id": "get_article" } ] }
```
Note the tool ids you intend to publish (for example `search_kb`, `get_article`); you will pick them in the allowlist step.

### 3. Operator: configure the MCP exposure allowlist (REST or console)
This step has no MCP tool. The operator does it out of band:

- In the console, open **MCP Server**, click **Enable** on the endpoint panel, then in the **Exposed tools** table tick the tools to publish (or use **Recommend safe defaults** for a read-only `get_*` / `list_*` / `find_*` set) and click **Save**.
- Or call the REST exposure endpoint directly to set the allowlist programmatically.

Saving replaces the previous allowlist atomically: any tool deselected vanishes from clients' palettes on the next request, and a client mid-call on a removed tool gets an error. See `mcp-exposure` for the endpoint and the allowlist semantics.

### 4. Operator: mint a scoped token (REST or console)
Also out of band: create an API token with the `mcp` scope (add `sessions:read` and similar only if the client truly needs them). Mint a separate token per client and store it securely; the token is shown once. This step has no MCP tool either.

### 5. Consumer: the external agent discovers the published tools
With the endpoint enabled, the allowlist saved, and a bearer token configured, the external client lists tools through the standard MCP discovery call:
```
tools/list
```
Response (abridged):
```json
{ "tools": [ { "name": "internal-kb::search_kb" }, { "name": "internal-kb::get_article" } ] }
```
Only the tools the operator put on the allowlist appear. The agent has no separate "expose" step; it simply sees what was published.

### 6. Consumer: call a published tool
The agent calls a discovered tool by its qualified name, for example:

`internal-kb::search_kb`
```json
{ "query": "onboarding checklist" }
```
Response:
```json
{ "items": [ { "id": "article-12", "title": "Onboarding checklist" } ] }
```
The call runs against the upstream connector through primer, scoped by the bearer token.

## Verify
`tools/list` from the external client returns exactly the tools the operator selected (and none of the deselected ones), and a call to one of them returns data from the internal service. The console MCP Server page shows the last-edited timestamp and the published tool count.

## Gotchas
- Exposure is allowlist-based and replace-on-save. Deselecting a tool removes it from every client immediately; coordinate before pruning a published tool that clients depend on.
- Scope tokens minimally. An `mcp`-scoped read-only assistant does not need write scopes; an over-scoped token is the most common source of unexpected agent behaviour.
- Registering the toolset (step 1) is necessary but not sufficient. Until the operator enables the endpoint and saves an allowlist that includes the tool, `tools/list` will not return it.

## Related
- `mcp-exposure`, `toolsets`, `tool-approval`
- `cookbook/create-and-run-a-session`
