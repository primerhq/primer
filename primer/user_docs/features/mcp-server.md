---
slug: mcp-server
title: MCP Server
section: features
summary: Expose a curated subset of primer's internal tools to external MCP clients via the built-in streamable-HTTP endpoint, and control exactly which tools are published through the allowlist.
---

## Concept

Primer ships a built-in MCP server at `/v1/mcp`. External MCP clients - Claude Desktop, Claude Code, claude.ai, or any client that speaks the MCP streamable-HTTP transport - connect to that URL and receive the set of tools you explicitly allow. Nothing is published by default; you enable the endpoint and then select tools one by one (or use the safe-defaults preset).

This is useful for two patterns:

1. **Using primer tools from Claude Desktop or Claude Code.** An agent in an external host can search your collections, list sessions, inspect agents, or post messages to chats - without leaving its native interface.
2. **Connecting remote automation.** A script or service that speaks MCP can call primer's internal tools over HTTP with a bearer token, without a browser session.

### The exposure model

The MCP exposure configuration is a single global record with two fields: a `enabled` boolean and an `allowed_tools` list. The list holds scoped tool ids of the form `toolset_id__tool_id` (for example, `search__search_agents` or `misc__get_agent_context`). Only tools in the list appear in the client's tool palette. Changing the list takes effect immediately on the next client request.

Not every tool can be added to the allowlist. Two categories are always excluded:

- **Yielding tools** - tools that park a session on an event bus (such as `subscribe_to_trigger` and `workspaces__watch_files`). MCP v1 has no pause/resume primitive, so a round-trip to a yielding tool is impossible over MCP.
- **Workspace session tools that need an agent session context** - tools that read the current `session_id` from the agent runtime context. These are meaningless outside an agent loop.

Tools from user-defined Toolset rows are also excluded. The MCP endpoint is for primer's own built-in toolsets, not for relaying to external MCP servers that you have mounted as toolsets.

Approval-gated tools are exposable from a catalogue standpoint, but the dispatcher refuses to invoke them when called over MCP. If a client calls an approval-gated tool, it receives an error rather than a park for a human decision. If you plan to expose tools to external clients, prefer tools that do not require approvals, or explicitly lift the approval requirement for those tools.

### Authentication

MCP clients authenticate with a bearer token. Tokens are created on the **API Tokens** page. Mint a dedicated token for each MCP client and grant only the scopes that client needs.

Primer is single-operator in v1. The first visit to the console presents a one-time registration screen that creates the only account. Subsequent visits show the login screen. Bearer tokens let automated clients (including MCP clients) authenticate without a browser session.

**Creating an API token for MCP:**

1. Navigate to **API Tokens** in the left nav.
2. Click **Create token**.
3. Enter a unique name (for example, `claude-desktop-mcp`). Names must be unique and at most 128 characters.
4. Under **Scopes**, check `mcp` to permit calls to the MCP endpoint. Check any additional scopes the client needs (for example, `sessions:write` if the client will start sessions).
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

Run:

```code-tabs:bash
--- bash
claude mcp add --transport streamable-http <your-primer-origin>/v1/mcp
```

Supply the bearer token when prompted, or add it manually to `~/.claude/claude_mcp_config.json`.

## What happens after

Once the endpoint is enabled and the allowlist is saved, connecting MCP clients receive the exact tools in the allowlist via `tools/list`. The names are the scoped ids (`toolset_id__tool_id`) to avoid collisions between toolsets.

When a client calls a tool (`tools/call`), primer:

1. Checks the allowlist. If the tool is not allowed, the client receives a `method-not-found` error.
2. Re-checks exposability. Yielding tools and session-context tools are refused even if they somehow appear in the allowlist.
3. Checks for an approval requirement. Approval-gated tools are refused with an error rather than parking for a human decision.
4. Dispatches to the toolset handler and returns the result.

The principal (the authenticated token's owner) and the tool call are logged for audit. The `updated_at` field on the exposure record changes every time the allowlist or enabled flag is saved, so the routing cache invalidates and the next `tools/list` returns the current set.

API tokens expire automatically when the expiry date passes. Revoked or expired tokens receive a 401 on all requests. The token row remains in the list for audit but the Revoke button becomes disabled.

```ref:features/toolsets-mcp
Mount external MCP servers as toolsets so primer's agents can call them.
```

```ref:features/toolsets-approvals
How the approval gate works and which tools require approval before execution.
```

```ref:reference/api-auth-tokens
Full token resource schema, list and create endpoints, and revoke endpoint.
```
