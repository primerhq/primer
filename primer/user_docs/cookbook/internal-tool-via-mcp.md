---
slug: internal-tool-via-mcp
title: Expose an internal tool via MCP
section: cookbook
summary: Publish a company-internal toolset to claude.ai's connector so remote agents reach your data.
difficulty: advanced
time_minutes: 30
tags: [mcp, agents, internal-collections]
prerequisites: [features/mcp-server]
features: [mcp, agent, internal-collections]
---

## Goal

Expose primer's tool catalogue to an external MCP client (Claude Desktop,
Claude Code, or claude.ai) so remote agents can call your internal tools
without leaving their native interface. This recipe walks through enabling
the MCP endpoint, selecting which tools to publish, and minting a scoped
token for the client.

## Prerequisites

- Primer is running and you can reach the console.
- At least one toolset is registered and its tools appear in the tool
  catalogue under Approvals / Policies.

## Steps

### 1. Enable the MCP endpoint

1. Open the console and navigate to **MCP Server** in the left nav.
2. In the **MCP server endpoint** panel, click **Enable**.
   The status pill changes to `enabled`.
3. Click **Copy URL** to copy the endpoint URL, or click
   **Copy Claude Desktop config** to get a ready-to-paste JSON snippet.

### 2. Choose which tools to publish

1. Scroll down to the **Exposed tools** table.
2. Use the toolset filter chips to narrow the list if you have many toolsets.
3. Check **Exposable only** to hide tools that cannot be published.
4. Tick the checkbox next to each tool you want to expose.
   Use **Recommend safe defaults** to pre-select a conservative read-only
   set (`get_*`, `list_*`, `find_*`, and a handful of pure-function tools).
5. Click **Save**. The endpoint panel caption updates with the new tool count.

```callout:warning
Saving a new allowlist replaces the previous one atomically. Any tool you
deselect is removed from the client's tool palette on the next request.
Clients mid-call on a removed tool receive an error.
```

### 3. Mint a scoped API token

The MCP client authenticates with a bearer token that has the `mcp` scope.

1. Navigate to **API tokens** in the left nav.
2. Click **Create token**.
3. Name the token (for example, `claude-desktop-prod`).
4. Under **Scopes**, check **mcp**. This is the scope the MCP endpoint
   requires, and it is currently the only scope the platform enforces.
5. Optionally set an expiry date.
6. Click **Create token**.

```embed:api-token-create
```

7. Click **Copy token** and store the value securely.
   The token is shown only once.

```callout:tip
Mint a separate token for each MCP client. Today the only enforced scope
is `mcp` (the MCP endpoint checks for it); other scope strings are
accepted but not yet enforced by any route, so do not rely on them as an
access-control boundary.
```

### 4. Connect the client

**Claude Desktop**

1. Open `~/Library/Application Support/Claude/claude_desktop_config.json`
   (create the file if it does not exist).
2. Paste the copied config snippet under the top-level `mcpServers` key.
3. Replace the `<YOUR_TOKEN>` placeholder with the token you just created.
4. Restart Claude Desktop. The published tools appear in the tool palette.

**Claude Code**

Run:

```
claude mcp add --transport streamable-http <your-primer-origin>/v1/mcp
```

Supply the bearer token when prompted, or add it manually to
`~/.claude/claude_mcp_config.json`.

## Result

The remote client lists the tools you exposed and can call them in any
conversation. The MCP Server page shows the last-edited timestamp and the
published tool count. Use **Recommend safe defaults** and the per-tool
checkboxes to adjust the allowlist at any time, then click **Save** to push
the change live.

## Automate it

```ref:reference/api-toolsets
REST endpoints for registering toolsets and managing the MCP exposure
allowlist programmatically.
```
