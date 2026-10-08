---
slug: web-tools
title: Web tools - search, fetch, raw HTTP and download
summary: The built-in web toolset (web_search, web_fetch, http_request, download), which tool to pick, and the outbound guard that refuses internal addresses.
related: [workspaces, tool-approval, mcp-exposure]
mcp_tools:
  - web::web_search
  - web::web_fetch
  - web::http_request
  - web::download
---

# Web tools

## Overview

Every primer deployment registers the built-in `web` toolset. It has four
tools:

- `web_search` - search the public web; returns `[{title, url, snippet}]`.
- `web_fetch` - read a page; returns clean markdown of its main content.
- `http_request` - raw HTTP (any method, headers, body); returns
  `{status, headers, body, truncated}` with the body capped at 1 MB.
- `download` - save a remote file into the session's workspace
  (workspace sessions only).

All four need the `user` role.

## Mental model

The tools run in the primer platform process, not in your workspace. The
platform sits on a network that can reach things the public cannot (cloud
metadata, the cluster API, databases, admin ports), so every outbound
connection made by `http_request`, `download` and the `local` web-fetch
provider passes an egress guard:

- the host name is resolved to all its addresses;
- if ANY address is loopback, private (10/8, 172.16/12, 192.168/16),
  link-local (169.254/16, which includes the cloud metadata address
  169.254.169.254), CGNAT (100.64/10), IPv6 ULA (fc00::/7) or link-local
  (fe80::/10), multicast, unspecified (0.0.0.0, ::) or reserved, or an
  IPv4-mapped form of one of these, the request is refused;
- otherwise the connection goes to the address that was checked, so a
  name cannot resolve to a public address for the check and an internal
  one for the connection;
- `http_request` and `download` do NOT follow redirects: `http_request`
  returns the 3xx response (read its `location` header and decide), and
  `download` fails on it. Only `web_fetch` (the local provider) follows
  redirects, and it re-checks every hop.

Only `http` and `https` URLs are accepted.

An operator can allow specific internal targets with the
`PRIMER_EGRESS_ALLOW` setting (a list of CIDRs, IPs or exact host
names). It is empty by default. You cannot change it from a tool call.

## MCP tools

| Tool | Use it for |
|------|------------|
| `web::web_search` | Finding pages. |
| `web::web_fetch` | Reading a human web page or document as markdown. |
| `web::http_request` | JSON APIs, webhooks, raw status and headers. |
| `web::download` | Saving a file into the workspace. |

## Workflows

Read a public JSON API:

```json
{"tool": "web::http_request", "arguments": {"url": "https://api.github.com/repos/python/cpython"}}
```

```json
{"status": 200, "headers": {"content-type": "application/json; charset=utf-8"}, "body": "{...}", "truncated": false}
```

A request to an internal address is refused before any connection is
opened:

```json
{"tool": "web::http_request", "arguments": {"url": "http://169.254.169.254/latest/meta-data/"}}
```

```text
http-request refused: 169.254.169.254 resolves to a private address (169.254.169.254); an operator can allow it with PRIMER_EGRESS_ALLOW
```

The result has `is_error: true`. `download` answers the same way
(`download refused: ...`) and writes nothing; `web_fetch` reports
`web-fetch not available: refused: ...` (or, with an aggregated fetch
config, falls back to a remote provider, which fetches from its own
network).

## Gotchas

- A refusal is not transient. Retrying the same URL, a redirecting URL,
  a short link, or a name that resolves to the same address gets the
  same answer. Ask the operator to add the target to
  `PRIMER_EGRESS_ALLOW` if the access is intended.
- A host with one public and one private address is refused as a whole.
- To reach a service inside your own workspace, use the workspace tools
  (for example a shell command in the workspace), not the web tools: the
  web tools run on the platform, not in the workspace.
- `http_request` and `download` do not follow redirects; `web_fetch`
  does, checking every hop. A redirect to an internal address fails on
  that hop.

## Related

- [workspaces](workspaces.md) - workspace sessions, where `download`
  writes.
- [tool-approval](tool-approval.md) - gate `http_request` behind an
  approval policy as well.
- [mcp-exposure](mcp-exposure.md) - exposing web tools to external MCP
  clients.
