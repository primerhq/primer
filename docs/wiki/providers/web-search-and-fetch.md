---
title: "Web search and fetch settings"
summary: "Admin: which provider answers web searches and which fetcher reads pages. Agents use the web tools."
audience: admin
order: 7
tools: [system__get_web_search_active_config, system__set_web_search_active_config, system__get_web_fetch_active_config, system__set_web_fetch_active_config]
related: [web/search-and-fetch, not-agent-callable]
---
## What it is
Web search and web fetch each route through one active setting per deployment. The setting names one provider (`single` mode) or an ordered list tried in turn (`aggregated` mode). This page is the admin side of that: the four tools that read and change the two settings. Agents use the web tools themselves, at [web search and fetch](web/search-and-fetch). Every tool on this page needs an admin.

## Concepts
- The stored `config` is `{"mode": "single", "provider_id": "<id>"}` or `{"mode": "aggregated", "provider_ids": ["<id>", ...]}`, tried in that order. A repeated id is kept once.
- The search provider rows are `duckduckgo` (no key), `tavily`, `firecrawl` and `exa` (each with an API key). The fetcher rows are `local` (in-process, no key), `jina`, `firecrawl` and `exa` (with a key).
- The rows are not agent tools: the provider rows, with their keys, are registered by an operator in the console or over REST, and no system tool creates or edits a row. See [not-agent-callable](not-agent-callable).
- In `aggregated` mode a search moves to the next provider when one answers a provider error, and a fetch moves on when a fetcher fails or returns too little text; the call fails only when every provider in the list fails.
- One choice per deployment: it answers every agent's `web_search` and `web_fetch` calls. Out of the box it is `single` with the built-in `DuckDuckGo` for search and the built-in `local` fetcher.
- The `get_` tools also return the provider ids a `set_` may name, up to 200 by id.
- The rows `DuckDuckGo` (search) and `local` (fetch) are seeded at bootstrap: a create with either id is a 409, and its delete is a 403. A row the active config names cannot be deleted at all (409): change the active config first.
- A new choice takes effect in every server process within 5 seconds: each process caches the setting for up to 5 seconds, and the `set_` tools do not clear that cache.
- A provider key with surrounding whitespace, a control character or a non-ASCII character (typically a pasted trailing newline) is refused before any request: the tool answers `web-search not available: <provider> api_key has surrounding whitespace or a control/non-ASCII character; re-enter it` (or the same under `web-fetch not available`). The fix is to re-enter the key, not to retry; an `aggregated` setting may still succeed through its next provider.

## How to
1. Read the current setting; the answer lists the ids a `set_` may name.
2. Choose one provider, or an ordered list with the preferred one first.
3. Read the setting again to confirm the stored choice.

## Tool calls
### Get the web search setting
`system__get_web_search_active_config` reads where searches go, and the provider ids a `set_` may name. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "get_web_search_active_config", "arguments": {}}
```

Reference: [`system__get_web_search_active_config`](reference/tools/system/get_web_search_active_config)

### Set the web search setting
`system__set_web_search_active_config` chooses one provider, or an ordered list.

```json
{"toolset_id": "system", "tool_name": "set_web_search_active_config", "arguments": {"config": {"mode": "aggregated", "provider_ids": ["tavily-1", "DuckDuckGo"]}}}
```

A single-provider choice:

```json
{"toolset_id": "system", "tool_name": "set_web_search_active_config", "arguments": {"config": {"mode": "single", "provider_id": "DuckDuckGo"}}}
```

Reference: [`system__set_web_search_active_config`](reference/tools/system/set_web_search_active_config)

### Get the web fetch setting
`system__get_web_fetch_active_config` reads where page fetches go, and the fetcher ids a `set_` may name.

```json
{"toolset_id": "system", "tool_name": "get_web_fetch_active_config", "arguments": {}}
```

Reference: [`system__get_web_fetch_active_config`](reference/tools/system/get_web_fetch_active_config)

### Set the web fetch setting
`system__set_web_fetch_active_config` chooses one fetcher, or an ordered list.

```json
{"toolset_id": "system", "tool_name": "set_web_fetch_active_config", "arguments": {"config": {"mode": "single", "provider_id": "local"}}}
```

Reference: [`system__set_web_fetch_active_config`](reference/tools/system/set_web_fetch_active_config)

## Gotchas
- Every tool on this page needs an admin; a run below the floor is a `forbidden`.
- A choice is live only after the 5 second cache: a search or fetch in those seconds may still go to the old provider.
- A `set_` with an unknown id (a typo, or a row deleted since you listed them) or an empty list is a `validation-error`, and nothing is stored. Read the ids with the matching `get_` first.
- A provider row the active config names cannot be deleted (409 `cascade_blocked`); the reserved rows `DuckDuckGo` and `local` cannot be created (409) or deleted (403).
- A bad key is refused before any request, and retrying does not help: re-enter the key. The refusal names no part of the key.
- `type=unavailable` from a `get_` means the setting row is missing: the server creates it at startup, so check the server logs.

## Related
[web search and fetch](web/search-and-fetch), [not-agent-callable](not-agent-callable).
