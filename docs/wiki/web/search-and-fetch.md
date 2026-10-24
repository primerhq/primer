---
title: "Search and fetch"
summary: "Search the web and read a page as clean markdown."
order: 1
tools: [web__web_search, web__web_fetch]
related: [web/http-and-downloads, providers/web-search-and-fetch]
---
## What it is
The built-in `web` toolset answers fact lookups and page reads: `web__web_search` runs a query against the active search engine and `web__web_fetch` reads a page and returns its main content as clean markdown. Both are user-tier tools. Which engine answers and which fetcher reads pages is an admin choice, made at [web search and fetch settings](providers/web-search-and-fetch); your calls stay the same either way.

## Concepts
- `web__web_search` takes `query` (free text), `count` (1 to 25, default 5) and `safe_search` (`off`, `moderate` or `strict`, default `moderate`). It answers a JSON array of `{title, url, snippet}`; a backend may return fewer hits than `count`.
- `web__web_fetch` takes `url` (an absolute http or https URL) and optional `max_chars` and `max_lines` caps on the returned markdown. The output starts with a `# <title>` line and a `Source: <final url>` line, then the main content of the page: navigation, sidebars and scripts removed. When the extraction comes back thin, a note says the page may require JavaScript rendering and points at a JS-capable fetch provider.
- Both tools route through the active setting the admin chose: one provider, or an ordered list in `aggregated` mode. A provider that fails (a bad key, a network error, a timeout) drops the call to the next one, and a fetch that yields little text escalates the same way. The call fails only when every provider in the list has failed.
- Out of the box the fetcher is the built-in `local` one: it fetches the page itself, follows at most 10 redirects, and checks every hop against the egress guard described at [HTTP requests and downloads](web/http-and-downloads).
- The search and fetch settings are cached per process for up to 5 seconds, so an admin's change lands within 5 seconds, not at once.

## How to
1. Run `web__web_search` with your query; raise `count` when 5 hits will not cover it.
2. Pick a result URL and read it with `web__web_fetch`; pass `max_chars` or `max_lines` for a long page.
3. For a JSON or API endpoint, or raw status, headers and bytes, use `web__http_request` instead (web/http-and-downloads).

## Tool calls
### Search the web
`web__web_search` runs the query against the active search provider.

```json
{"toolset_id": "web", "tool_name": "web_search", "arguments": {"query": "python 3.13 release notes", "count": 5}}
```

Reference: [`web__web_search`](reference/tools/web/web_search)

### Fetch a page
`web__web_fetch` reads the page and returns its main content as markdown.

```json
{"toolset_id": "web", "tool_name": "web_fetch", "arguments": {"url": "https://docs.python.org/3/whatsnew/3.13.html", "max_chars": 4000}}
```

Reference: [`web__web_fetch`](reference/tools/web/web_fetch)

## Gotchas
- Read pages with `web__web_fetch`; use `web__http_request` for APIs and raw responses. A search hit is a pointer, not the page.
- A provider key with surrounding whitespace, a control character or a non-ASCII character (typically a pasted trailing newline) is refused before any request: the call answers `web-search not available: <provider> api_key has surrounding whitespace or a control/non-ASCII character; re-enter it` (the same shape under `web-fetch not available`). Retrying does not help: the key itself must be re-entered. Tell an admin, the fix is at [web search and fetch settings](providers/web-search-and-fetch).
- A single misconfigured provider fails the call with `web-search not available: ...` or `web-fetch not available: ...`. An aggregated list that exhausts every provider fails with `web-search failed: all N providers failed: ...` or `web-fetch failed: ...`, naming each provider's error.
- An egress refusal inside an aggregated fetch setting (the local fetcher reaches a private address) drops to the next provider, which may fetch from its own network; a single `local` setting answers `web-fetch not available: refused: ...` instead.

## Related
[HTTP requests and downloads](web/http-and-downloads), [web search and fetch settings](providers/web-search-and-fetch).
