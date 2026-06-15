---
slug: web-fetch-http
title: Web Fetch & HTTP Fetch
section: web
summary: The web__web_fetch and web__http_request tools for reading a web page as clean markdown or calling an HTTP/API endpoint and returning the raw response.
---

## Two URL tools

The `web` toolset has two tools that act on a URL you already have. (To discover a URL first, use `web__web_search`, covered separately.)

- **`web__web_fetch`** reads a human-facing web page or document and returns clean markdown of its main content, with navigation, sidebars, and scripts stripped out.
- **`web__http_request`** performs an HTTP request and returns the raw response status, headers, and body as JSON. Use it for JSON/API endpoints, webhooks, or when you need to inspect raw status and headers.

Pick `web_fetch` to read a page and `http_request` to call an endpoint.

## web_fetch

Fetches a URL and returns markdown of the page's main content. The output is a small header (`# <title>` when the page has one, plus a `Source: <final-url>` line) followed by the extracted markdown. When extraction yields very little text, primer appends a note that the page may require JavaScript rendering and suggests configuring a JS-capable web-fetch provider.

| Parameter | Required | Type | Notes |
|---|---|---|---|
| `url` | Yes | string (http/https) | Absolute URL of the page to read. |
| `max_chars` | No | integer > 0 | Cap the returned markdown to this many characters. |
| `max_lines` | No | integer > 0 | Cap the returned markdown to this many lines. |

Machine metadata (HTTP status, content type, final URL after redirects, character and line counts, and whether the output was truncated by a limit or judged thin) is returned alongside the markdown as extended fields.

Web fetch routes through a configured web-fetch provider, so behaviour and JavaScript support depend on which provider is active.

## http_request

Performs a single HTTP request and returns a JSON object with the response.

| Parameter | Required | Type | Notes |
|---|---|---|---|
| `url` | Yes | string (http/https) | Absolute URL to request. |
| `method` | No | enum | One of GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS. Defaults to GET. |
| `headers` | No | object | Optional request headers; keys and values are strings. |
| `body` | No | string | Optional request body. Serialise structured payloads (JSON, form encoding) yourself before the call. |
| `timeout_seconds` | No | number | Per-request timeout, greater than 0 and at most 300. Defaults to 30. |

The response is JSON with four fields: `status` (the HTTP status code), `headers` (the response headers), `body` (the response body as text), and `truncated` (a boolean).

### Output truncation

The response body is capped at a configurable byte limit. The default cap is 1 MB (1,000,000 bytes). When the body exceeds the cap it is truncated to the cap and `truncated` is set to `true`, so the agent can tell that more content was available. The cap is configured per deployment.

```ref:web/web-search-providers
Configure the web search providers that back web_search, the discovery counterpart to these two tools.
```

```ref:features/mcp-server
Exposing web tools (and others) to external clients when primer runs as an MCP server.
```
