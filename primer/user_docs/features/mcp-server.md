---
slug: mcp-server
title: MCP Server
section: features
summary: Expose primer's own platform capabilities over a built-in MCP endpoint so external agents can use primer as a backend to build and run agentic AI systems, with an allowlist that controls exactly which tools are published.
---

## Concept

Primer ships a built-in MCP server at `/v1/mcp`. The point of that endpoint is to turn the whole platform into a toolbox that an external agent can drive. Connect Claude Code, Claude Desktop, claude.ai, or any other MCP-speaking agentic system to that URL and primer's own capabilities become tools the external agent can call: create and configure agents, wire up graphs, spin up workspace sessions and run them, manage collections, channels, and triggers, and run semantic search across the platform. In short, primer-as-an-MCP-server lets an outside agent use primer as a backend to **build and run agentic AI systems** without ever touching primer's REST API or console directly.

This is primer exposing *its own* platform surface to the outside. It is the inverse of mounting external MCP servers as toolsets so primer's own agents can call them; for that direction, see the [toolsets over MCP](toolsets-mcp) feature. This page is only about the server primer hosts.

The tools that become available are primer's built-in toolsets:

- **`system`** - CRUD over the core entities (agents, graphs, collections, channels, providers, approval policies, and more) plus `system__invoke_agent` and `system__invoke_graph` to run them, `system__search_collection`, and document operations.
- **`workspaces`** - create, run, steer, and cancel workspace sessions (`workspaces__create_workspace_session`), and read or write workspace files.
- **`search`** - semantic search across agents, graphs, collections, and tools (`search__search_agents`, `search__search_collections`).
- **`trigger`** - manage triggers and subscriptions.
- **`web`** - web search and HTTP requests.
- **`misc`** and **`harness`** - utility and harness-management helpers.

An external agent that holds a token for these tools can, for example, search for an existing agent, create a new one from a prompt, then invoke it and read the result, all over MCP. That is the platform's actual dogfooding pattern: primer's own dogfood instance registers its MCP endpoint with Claude Code and is driven entirely through these tools.

### The exposure model

Nothing is published by default. Exposure is governed by a single global `McpExposure` record with two fields: an `enabled` boolean and an `allowed_tools` list. The list holds scoped tool ids of the form `toolset_id__tool_id` (for example, `system__invoke_agent` or `search__search_agents`). Only tools in the allowlist appear on the client's `tools/list`, and only while `enabled` is true. A fresh install starts with `enabled=false` and an empty allowlist, so the endpoint publishes zero tools until you opt in. Editing the allowlist takes effect on the next client request.

The allowlist is operator-controlled and mutated only from the console (cookie session); a bearer token cannot change it even with the `mcp` scope. Saving a new allowlist replaces the previous one atomically.

Not every tool can be added to the allowlist. Three categories are always excluded:

- **Tools from user-defined Toolset rows.** The endpoint exposes only primer's reserved built-in toolsets (`system`, `workspaces`, `search`, `trigger`, `web`, `misc`, `harness`, and `workspace_ext`). Tools from toolsets you defined yourself, including external MCP servers you mounted, are denied with reason `not_system_toolset`. They belong to primer's own agents, not to outside MCP clients.
- **Yielding tools** - tools that park a session on an event bus (such as `misc__ask_user`, `misc__sleep`, `trigger__subscribe_to_trigger`, and `workspaces__watch_files`). MCP v1 has no pause/resume primitive, so a round-trip is impossible. These are denied with reason `yielding_unsupported`.
- **Workspace tools that need an active agent session** - workspace tools that read the current `session_id` from the agent runtime context. These are meaningless outside an agent loop and are denied with reason `needs_session`.

There is no policy-level denylist beyond those technical floors: you enabled MCP, you chose which tools to expose, and you minted the token, so you choose the risk surface (including powerful tools like `system__call_tool` or `web__http_request`).

Approval-gated tools are exposable from a catalogue standpoint, but the dispatcher refuses to invoke them when called over MCP. If a client calls an approval-gated tool, it receives an error rather than a park for a human decision. If you plan to expose tools to external clients, prefer tools that do not require approvals, or explicitly lift the approval requirement for those tools.

### Authentication

MCP clients authenticate with a bearer token. Tokens are created on the **API Tokens** page. Mint a dedicated token for each MCP client and grant only the scopes that client needs.

Primer is single-operator in v1. The first visit to the console presents a one-time registration screen that creates the only account. Subsequent visits show the login screen. Bearer tokens let automated clients (including MCP clients) authenticate without a browser session.

**Creating an API token for MCP:**

1. Navigate to **API Tokens** in the left nav.
2. Click **Create token**.
3. Enter a unique name (for example, `claude-desktop-mcp`). Names must be unique and at most 128 characters.
4. Under **Scopes**, check `mcp` to permit calls to the MCP endpoint. This is currently the only scope the platform enforces; other scope strings are accepted but not yet checked by any route.
5. Optionally set an expiry date.
6. Click **Create token**.

The dialog switches to a one-time reveal:

```embed:api-token-create
```

7. Click **Copy token** to copy the plaintext to your clipboard.
8. Click **I have saved it, close** once you have stored the value.

The token is never shown again. If you lose it, revoke it and create a new one.

To revoke a token, click **Revoke** on the token's row and confirm. The token stops working immediately. The row remains in the list for audit purposes.

## Configuration

### MCP exposure fields

| Field | Notes |
|---|---|
| **Enabled** | Whether the `/v1/mcp` endpoint accepts connections. When disabled, clients receive a connection error. |
| **Allowed tools** | The allowlist of scoped tool ids (`toolset_id__tool_id`). Only these tools appear in the client palette. |

### API token fields

| Field | Notes |
|---|---|
| **Name** | Unique label. At most 128 characters. |
| **Scopes** | Comma-separated scope strings. `mcp` is required for MCP endpoint access. |
| **Expires at** | Optional expiry datetime. Leave blank for a non-expiring token. |

## Walkthrough

### Enabling the endpoint

1. Navigate to **MCP Server** in the left nav.
2. In the **MCP server endpoint** panel, click **Enable**. The status pill changes to `enabled`.
3. Click **Copy URL** to copy the endpoint URL (`<your-primer-origin>/v1/mcp`), or click **Copy Claude Desktop config** to copy a ready-to-paste JSON snippet for `~/Library/Application Support/Claude/claude_desktop_config.json`.
4. Replace the `<YOUR_TOKEN>` placeholder in the config with a token from the API Tokens page that has the `mcp` scope.

```embed:mcp-exposure
```

### Choosing which tools to expose

```callout:tip
The Exposed tools table shows every tool in the catalogue with an `exposable` or blocked status. Blocked tools show a reason in the Status column and cannot be added to the allowlist.
```

1. Use the toolset filter chips at the top of the **Exposed tools** table to narrow by toolset.
2. Check **Exposable only** to hide non-exposable rows.
3. Tick the checkbox next to each tool you want to publish. The header checkbox selects or deselects all currently visible exposable rows.
4. Use **Recommend safe defaults** to pre-select a conservative read-only set (search toolset tools, `get_*`, `list_*`, `find_*`, and a handful of pure-function misc tools). This only stages the selection - nothing is saved until you click **Save**.
5. Click **Save** to publish the updated allowlist.

Click **Reset** to discard staged changes before saving.

```callout:warning
Saving a new allowlist replaces the previous one atomically. Any tool you deselect is removed from the client's tool palette on the next request. Clients mid-call on a removed tool receive an error.
```

### Connecting Claude Desktop

1. Copy the Claude Desktop config from the MCP Server page.
2. Open `~/Library/Application Support/Claude/claude_desktop_config.json` (create it if it does not exist).
3. Paste the config under the top-level `mcpServers` key.
4. Replace `<YOUR_TOKEN>` with a real token that has the `mcp` scope.
5. Restart Claude Desktop. The published tools appear in Claude's tool palette.

### Connecting Claude Code

Register primer's endpoint as an MCP server. This is exactly how primer's own dogfood instance is driven:

```code-tabs:bash
--- bash
claude mcp add --transport http primer <your-primer-origin>/v1/mcp
```

Supply the bearer token (with the `mcp` scope) when prompted, or add it manually to your Claude Code MCP config. Use the trailing-slash form of the URL if you call the endpoint directly over JSON-RPC; a bare `/v1/mcp` may redirect.

After Claude Code reconnects, the published tools appear in its palette. From there an external agent uses primer as a backend to build and run an agentic system. For example, the agent can search for an existing agent, create a new one, then invoke it and read the result:

```code-tabs:json
--- json
{ "method": "tools/call",
  "params": { "name": "search__search_agents", "arguments": { "query": "triage incoming support tickets" } } }
{ "method": "tools/call",
  "params": { "name": "system__create_agent",
    "arguments": { "id": "ticket-triage", "model": "...", "system_prompt": "You triage support tickets..." } } }
{ "method": "tools/call",
  "params": { "name": "system__invoke_agent",
    "arguments": { "agent_id": "ticket-triage", "input": "Customer reports a billing error." } } }
```

The same pattern wires graphs (`system__create_graph` then `system__invoke_graph`), spins up workspace sessions (`workspaces__create_workspace_session`), and manages channels and triggers, all without leaving the external agent's interface.


```ref:toolsets/toolsets-external
Mount external MCP servers as toolsets so primer's agents can call them.
```

```ref:toolsets/toolsets-approvals
How the approval gate works and which tools require approval before execution.
```

```ref:reference/api-auth-tokens
Full token resource schema, list and create endpoints, and revoke endpoint.
```
