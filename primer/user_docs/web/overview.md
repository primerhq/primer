---
slug: web-overview
title: Web
section: web
summary: The web toolset gives agents two ways to reach the internet: searching the public web for pages, and fetching or calling a specific URL.
---

## Search versus fetch

The always-on `web` toolset gives agents two distinct capabilities:

- **Search** (`web__web_search`) finds pages across the public web and returns title/url/snippet hits. Use it when you do not yet have a URL. Web search routes through a configured web search provider.
- **Fetch and HTTP** (`web__web_fetch`, `web__http_request`) act on a URL you already have. `web_fetch` reads a human web page and returns clean markdown; `http_request` calls a JSON or API endpoint and returns the raw status, headers, and body.

A common pattern is search first to discover a page, then `web_fetch` to read it.

```ref:web/web-search-providers
Register and order the web search providers that back web_search.
```

```ref:web/web-fetch-http
The web_fetch and http_request tools: parameters, output, and the byte cap.
```
