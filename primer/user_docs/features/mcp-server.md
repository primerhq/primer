---
slug: mcp-server
title: MCP server
section: features
summary: Expose primer's tools to external MCP clients via the built-in MCP server, and control which tools are published.
---

## Overview

The MCP Server page in the console controls two things: whether primer's built-in MCP server is active, and which tools from the tool catalogue are published to connecting clients. External MCP clients -- Claude Desktop, Claude Code, claude.ai, or any client that speaks streamable-HTTP MCP -- connect to `/v1/mcp` and receive only the tools you explicitly allow.

The page is split into two panels: **MCP server endpoint** (the enable/disable toggle and copy helpers) and **Exposed tools** (the per-tool allowlist).

## Enabling the endpoint

1. Navigate to **MCP Server** in the left nav.
2. In the **MCP server endpoint** panel, click **Enable**. The status pill changes to `enabled`.
3. Copy the endpoint URL using **Copy URL**, or copy the full Claude Desktop config JSON using **Copy Claude Desktop config**.
   - The config snippet is ready to paste under `mcpServers` in `~/Library/Application Support/Claude/claude_desktop_config.json`. Replace the `<YOUR_TOKEN>` placeholder with a token from the API Tokens page. The token must have the `mcp` scope.
4. Paste the URL or config into your MCP client and connect.

The endpoint URL is always `<your-primer-origin>/v1/mcp`. The **Last edited** caption shows who last changed the configuration and when.

```callout:tip
Mint a dedicated API token for each MCP client and scope it to the minimum permissions that client needs. Granting `sessions:write` to a read-only assistant is the most common source of unexpected behavior.
```

## Choosing which tools to expose

All tools in the catalogue appear in the **Exposed tools** table. Only tools with the `exposable` status can be added to the allowlist; tools marked with a blocking reason (shown in the Status column) cannot be published regardless of the toggle.

1. Use the toolset filter chips at the top of the table to narrow by toolset.
2. Check **Exposable only** to hide non-exposable rows.
3. Tick the checkbox next to each tool you want to publish. The header checkbox selects or deselects all currently visible exposable rows at once.
4. Use **Recommend safe defaults** to pre-select a conservative read-only set (search toolset tools, `get_*`, `list_*`, `find_*`, and a handful of pure-function misc tools). This only stages the selection; nothing is saved until you click **Save**.
5. Click **Save** to publish the updated allowlist. The endpoint panel caption updates to reflect the new count.

Click **Reset** to discard staged changes before saving.

```callout:warning
Saving a new allowlist replaces the previous one atomically. Any tool you deselect is immediately removed from the client's tool palette on the next request. Clients currently mid-call on a removed tool will receive an error.
```

## Connecting Claude Desktop

After enabling the endpoint and copying the config:

1. Open `~/Library/Application Support/Claude/claude_desktop_config.json` (create it if it does not exist).
2. Paste the copied JSON under the top-level `mcpServers` key.
3. Replace `<YOUR_TOKEN>` with a real API token that has the `mcp` scope.
4. Restart Claude Desktop. The published tools appear in Claude's tool palette.

For Claude Code, run `claude mcp add --transport streamable-http <your-primer-origin>/v1/mcp` and supply the bearer token when prompted, or add it manually to `~/.claude/claude_mcp_config.json`.

## Automate this

```ref:reference/api-toolsets
REST endpoints for registering and managing toolsets (for mounting external MCP servers as toolsets).
```

```ref:reference/mcp-server-reference
The full MCP server wire protocol, exposure PUT shape, and scope requirements.
```

```ai-doc:mcp-exposure
```
