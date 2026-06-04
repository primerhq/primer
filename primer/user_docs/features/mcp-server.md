---
slug: mcp-server
title: MCP server
section: features
summary: Expose primer to MCP clients like claude.ai; tool selection, scopes, OAuth, and the wire envelope.
---

## What MCP exposure does

Primer ships its own MCP server. Connecting an MCP client (the
claude.ai connector, a local Claude Code session, etc.) lets the
client treat primer's tools as native MCP tools. The agent on
the other end picks them out of the tool palette and calls them
with the same JSON-schema discipline as any other MCP tool.

The exposed surface is a subset of primer's tool catalogue: the
operator picks which toolsets are reachable from outside, and
the MCP server publishes only those.

## The topbar indicator

The top bar shows an MCP indicator when at least one client is
connected. The dot turns green when the connection is healthy.

```mockup:topbar
{ "workers": "5/8", "inFlight": "1 in flight", "showThemeToggle": true }
```

## Exposing toolsets

Toolset exposure is per-instance; flip the toggle on the MCP
Server page to publish or hide. Hidden toolsets are not in the
client's tool palette at all.

```code-tabs:bash,json
--- bash
# Toggle via the REST surface (matches what the console does):
curl -X PUT https://primer.example/v1/mcp/exposure/system \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"exposed":true}'
--- json
{
  "instance": "primer.example",
  "exposed_toolsets": ["system", "search", "knowledge"],
  "scopes": ["agents:read", "sessions:read", "sessions:write"]
}
```

## Connecting claude.ai

Open claude.ai/customize/connectors and paste primer's MCP URL.
The flow asks for an OAuth bearer token (mint one on the API
Tokens page) and writes it into the connector record. claude.ai
then connects, lists the published tools, and includes them in
the model's tool palette for the next conversation.

```callout:tip
Pick the scope set on the API token to match what you actually
want the remote agent to do. Granting `sessions:write` to a
read-only assistant is the most common 'why does this work in
ways I did not expect' surprise.
```

## Auth and rate limits

Every MCP call carries the operator's bearer token. The token's
scopes gate the call; an unscoped tool call returns 403. The MCP
server rate-limits per-token at 10 requests per second by
default; bump via `PRIMER_MCP__RATE_LIMIT_PER_SECOND`.
