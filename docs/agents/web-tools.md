---
slug: web-tools
title: Web tools - search, fetch, raw HTTP and download
summary: The built-in web toolset (web_search, web_fetch, http_request, download), which tool to pick, the outbound guard that refuses internal addresses, and the admin tools that choose which providers answer web searches and page fetches.
related: [workspaces, tool-approval, mcp-exposure]
mcp_tools:
  - web::web_search
  - web::web_fetch
  - web::http_request
  - web::download
  - system::get_web_search_active_config
  - system::set_web_search_active_config
  - system::get_web_fetch_active_config
  - system::set_web_fetch_active_config
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

**Which provider answers.** `web_search` and `web_fetch` do not pick a
provider themselves: the deployment's active config does, one for search
and one for fetch. It has two modes:

- `single`: every call goes to one provider;
- `aggregated`: an ordered list. The first provider is tried, and the
  next one is used when it fails (for `web_fetch`, also when it returns
  too little text, such as a page that needs JavaScript).

Out of the box both are `single`: the built-in `DuckDuckGo` for search and
the built-in `local` fetcher for fetch. The choice is per deployment, not
per agent or session. An admin agent reads it with
`system::get_web_search_active_config` and
`system::get_web_fetch_active_config` (both also list the provider ids
that may be named) and changes it with
`system::set_web_search_active_config` and
`system::set_web_fetch_active_config`. Over REST it is
`GET` and `PUT /v1/web_search_active_config`, and
`GET` and `PUT /v1/web_fetch_active_config`. In the console an operator
sets both on the Platform › Web page. The providers themselves
(with their API keys) are registered by an operator, in the console or
with `POST /v1/web_search_providers` and `POST /v1/web_fetch_providers`;
no tool creates them.

## MCP tools

| Tool | Use it for |
|------|------------|
| `web::web_search` | Finding pages. |
| `web::web_fetch` | Reading a human web page or document as markdown. |
| `web::http_request` | JSON APIs, webhooks, raw status and headers. |
| `web::download` | Saving a file into the workspace. |
| `system::get_web_search_active_config` | Reading where web searches go, and the provider ids a set may name (admin). |
| `system::set_web_search_active_config` | Choosing one search provider, or an ordered list (admin). |
| `system::get_web_fetch_active_config` | Reading where page fetches go, and the fetcher ids a set may name (admin). |
| `system::set_web_fetch_active_config` | Choosing one fetcher, or an ordered list (admin). |

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

Point page fetches at an ordered list (admin). Read the current choice
and the fetchers that exist:

```json
{"tool": "system::get_web_fetch_active_config", "arguments": {}}
```

```json
{"config": {"mode": "single", "provider_id": "local"}, "providers": [{"id": "jina-1", "provider_type": "jina"}, {"id": "local", "provider_type": "local"}]}
```

Then set the list, first choice first:

```json
{"tool": "system::set_web_fetch_active_config", "arguments": {"config": {"mode": "aggregated", "provider_ids": ["jina-1", "local"]}}}
```

```json
{"config": {"mode": "aggregated", "provider_ids": ["jina-1", "local"]}}
```

Page fetches now try `jina-1` first and fall back to `local`. An id that
is not a provider is refused, and nothing is stored:

```json
{"tool": "system::set_web_fetch_active_config", "arguments": {"config": {"mode": "single", "provider_id": "jina-2"}}}
```

```json
{"type": "validation-error", "message": "active config references unknown provider id(s): ['jina-2']"}
```

## Gotchas

- A search or fetch provider whose API key has surrounding whitespace, a control character or a non-ASCII character (typically a key pasted with its trailing newline) is refused BEFORE any request: the tool answers `web-search not available: <provider> api_key has surrounding whitespace or a control/non-ASCII character; re-enter it` (`web-fetch not available: ...`). Retrying does not help: the key itself must be fixed, so tell the operator to re-enter that provider's API key. An aggregated config falls back to its next provider, so the call can still succeed; the refusal names no part of the key.
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
- `http_request`'s `timeout_seconds` is the deadline of the WHOLE call, the
  request and the response body, not of one network read: a server that
  sends a byte at a time is given up on at that deadline
  (`http-request timed out after Ns`), and `web_fetch` gives up on the same
  terms (a transient failure, so an aggregated fetch tries the next
  provider). `download` has the same kind of deadline, fixed by the platform
  at 300 seconds and not by an argument: past it the call fails
  with `download timed out after Ns; nothing was written`.
- A redirect whose `Location` cannot be parsed (`data:...`, a malformed
  `xn--` host) is a failed request (`http-request failed: ...`,
  `download failed: ...`), not a crash of the turn.
- A response body is read only up to the tool's cap, counting the bytes after
  decompression: `http_request` returns the first 1 MB and sets
  `truncated`; `download` refuses a file over its cap and writes nothing.
  The tools accept ONE `gzip` or `deflate` layer (they ask for no other);
  a response with any other `Content-Encoding` (`br`, `zstd`, a stacked
  `gzip, gzip`) is refused with `unsupported content-encoding`. Pass your
  own `Accept-Encoding` header to `http_request` only if you can accept that.
  A compressed body with data after the end of its stream (trailing bytes,
  a second gzip member), one that ends before it, or one far larger on the
  wire than its cap allows, is a failed request, never a partial result.
- `download`'s `max_bytes` can only LOWER the platform's fixed 100 MB cap;
  a larger value is ignored and the 100 MB cap applies.
- The four `system::*_active_config` tools need the admin role. A run
  started by anyone else gets `access denied` and nothing changes.
- A new web search or web fetch choice takes effect within 5 seconds, not
  at once: every server process caches the setting for up to 5 seconds,
  and the set tools do not clear that cache. A search or fetch made in
  those seconds may still go to the old provider.
- A set refuses, with `type=validation-error`, an id that is not a
  provider (a typo, or one deleted since you listed them) and an
  aggregated list with no ids; nothing is stored then. A repeated id is
  stored once. Read the ids with the matching `get_` tool first.

## Related

- [workspaces](workspaces.md) - workspace sessions, where `download`
  writes.
- [tool-approval](tool-approval.md) - gate `http_request` behind an
  approval policy as well.
- [mcp-exposure](mcp-exposure.md) - exposing web tools to external MCP
  clients.
