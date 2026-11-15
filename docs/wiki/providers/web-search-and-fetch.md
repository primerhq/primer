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
- Provider registration, updates and tests also have generated route-backed tools; the operation references below describe them. They retain their admin floor, secret masking and effective approval gates. Read a provider back and test its actual behavior before selecting it.
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

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`web_fetch__get_active_config`](reference/tools/web_fetch/get_active_config) | Read the singleton active web fetch config. | user |
| [`web_fetch__list_provider_types`](reference/tools/web_fetch/list_provider_types) | Provider-type metadata for the UI form. Returns the field shape the operator needs to fill in per provider type. | user |
| [`web_fetch__put_active_config`](reference/tools/web_fetch/put_active_config) | Replace the singleton active web fetch config. | admin |
| [`web_fetch__test_provider`](reference/tools/web_fetch/test_provider) | Test a draft provider config by performing a one-shot fetch. Builds a transient adapter, runs fetch(url='https://example.com'), then discards. Returns {ok, title, chars} or {ok=false, error}. | admin |
| [`web_fetch_providers__create`](reference/tools/web_fetch_providers/create) | Create WebFetchProvider. | admin |
| [`web_fetch_providers__delete`](reference/tools/web_fetch_providers/delete) | Delete WebFetchProvider. | admin |
| [`web_fetch_providers__find`](reference/tools/web_fetch_providers/find) | Find WebFetchProvider with predicate. | admin |
| [`web_fetch_providers__get`](reference/tools/web_fetch_providers/get) | Get WebFetchProvider by id. | user |
| [`web_fetch_providers__list`](reference/tools/web_fetch_providers/list) | List WebFetchProvider. | user |
| [`web_fetch_providers__update`](reference/tools/web_fetch_providers/update) | Replace WebFetchProvider. | admin |
| [`web_search__get_active_config`](reference/tools/web_search/get_active_config) | Read the singleton active web search config. | user |
| [`web_search__list_provider_types`](reference/tools/web_search/list_provider_types) | Provider-type metadata for the UI form. Returns the field shape the operator needs to fill in per provider type. | user |
| [`web_search__put_active_config`](reference/tools/web_search/put_active_config) | Replace the singleton active web search config. | admin |
| [`web_search__test_provider`](reference/tools/web_search/test_provider) | Test a draft provider config by performing a one-shot search. Builds a transient adapter, runs search(query='primer', count=1, safe_search='moderate'), then discards. Returns {ok, hits} or {ok=false, error}. | admin |
| [`web_search_providers__create`](reference/tools/web_search_providers/create) | Create WebSearchProvider. | admin |
| [`web_search_providers__delete`](reference/tools/web_search_providers/delete) | Delete WebSearchProvider. | admin |
| [`web_search_providers__find`](reference/tools/web_search_providers/find) | Find WebSearchProvider with predicate. | admin |
| [`web_search_providers__get`](reference/tools/web_search_providers/get) | Get WebSearchProvider by id. | user |
| [`web_search_providers__list`](reference/tools/web_search_providers/list) | List WebSearchProvider. | user |
| [`web_search_providers__update`](reference/tools/web_search_providers/update) | Replace WebSearchProvider. | admin |

## Related
[web search and fetch](web/search-and-fetch), [not-agent-callable](not-agent-callable).
