---
title: "Semantic-search providers"
summary: "Admin: register the vector stores that hold collection embeddings."
audience: admin
order: 5
tools: [system__list_semantic_search_providers, system__get_semantic_search_provider, system__create_semantic_search_provider, system__update_semantic_search_provider, system__delete_semantic_search_provider, system__find_semantic_search_providers, system__invalidate_semantic_search_provider]
related: [providers/embedding, knowledge/search]
---
## What it is
A `SemanticSearchProvider` row is a vector store: where a collection's embeddings live. A collection's search block names exactly one row, in `search.vector_store_provider_id`; without that block the collection is grep-only. Every tool on this page needs an admin.

## Concepts
- `provider` is `pgvector`, `pgvectorscale` or `lance`, and `config` must match it. The two Postgres backends take `hostname`, `username`, `password` and `database` (`port` defaults to 5432), plus tuning knobs: HNSW parameters, `db_schema`, a `pool` block, the distance metric, a reindex cron, and `use_halfvec` (`enable_diskann` and its knobs on `pgvectorscale`). `lance` takes an absolute `path`: the directory holding the datasets, created on initialise if missing.
- The `id` is optional on create: the server assigns `semantic-search-provider-<hex>`, and it is immutable after creation.
- The `password` is write-only: it comes back masked on every read, and a mask sent back unchanged keeps the stored secret, only when the origin (scheme, host, port) is unchanged.
- Each process keeps one live backend per row, cached. An update or a delete through these tools drops the cached adapter in the process that ran it at once; the next request rebuilds it from the row's current config.
- The id `lance` is reserved: bootstrap seeds the local LanceDB row under it. A create with that id is a 409, and its delete is a 403.

## How to
1. Register the store: a reachable Postgres for the Postgres backends, a writable directory for `lance`.
2. Name the row in a collection's search block, next to the collection's embedder.
3. Change the row's config when the backend moves, then invalidate its cached adapter so the next search request rebuilds it.

## Tool calls
### Create a provider
`system__create_semantic_search_provider` registers the vector store. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_semantic_search_provider", "arguments": {"entity": {"id": "pg-1", "provider": "pgvector", "config": {"hostname": "db.internal", "username": "primer", "password": "REPLACE", "database": "primer"}}}}
```

Reference: [`system__create_semantic_search_provider`](reference/tools/system/create_semantic_search_provider)

### Get a provider
`system__get_semantic_search_provider` fetches one row by id, the password masked.

```json
{"toolset_id": "system", "tool_name": "get_semantic_search_provider", "arguments": {"id": "pg-1"}}
```

Reference: [`system__get_semantic_search_provider`](reference/tools/system/get_semantic_search_provider)

### Update a provider
`system__update_semantic_search_provider` replaces the whole row: pass the id and the full entity. The cached adapter for the row is dropped in the process that runs it.

```json
{"toolset_id": "system", "tool_name": "update_semantic_search_provider", "arguments": {"id": "pg-1", "entity": {"id": "pg-1", "provider": "pgvector", "config": {"hostname": "db.internal", "port": 5432, "username": "primer", "password": "REPLACE", "database": "primer"}}}}
```

Reference: [`system__update_semantic_search_provider`](reference/tools/system/update_semantic_search_provider)

### Find providers
`system__find_semantic_search_providers` filters rows by field values.

```json
{"toolset_id": "system", "tool_name": "find_semantic_search_providers", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "provider"}, "op": "=", "right": {"kind": "value", "value": "pgvector"}}}}
```

Reference: [`system__find_semantic_search_providers`](reference/tools/system/find_semantic_search_providers)

### Delete a provider
`system__delete_semantic_search_provider` removes the row and drops its cached adapter in the process that runs it.

```json
{"toolset_id": "system", "tool_name": "delete_semantic_search_provider", "arguments": {"id": "pg-1"}}
```

Reference: [`system__delete_semantic_search_provider`](reference/tools/system/delete_semantic_search_provider)

### List providers
`system__list_semantic_search_providers` browses the rows as a page.

```json
{"toolset_id": "system", "tool_name": "list_semantic_search_providers", "arguments": {"limit": 20}}
```

Reference: [`system__list_semantic_search_providers`](reference/tools/system/list_semantic_search_providers)

### Invalidate a provider's adapter
`system__invalidate_semantic_search_provider` evicts the cached backend for one row so the next request rebuilds it from the row's current config. It is a no-op when nothing is cached.

```json
{"toolset_id": "system", "tool_name": "invalidate_semantic_search_provider", "arguments": {"id": "pg-1"}}
```

Reference: [`system__invalidate_semantic_search_provider`](reference/tools/system/invalidate_semantic_search_provider)

## Gotchas
- Every tool on this page needs an admin; a run below the floor is a `forbidden`.
- Deleting a row does not check its collections: the reference lives in the collection's search block, and a broken reference surfaces through that block's error state. Re-point or delete the collection first.
- The reserved id `lance` is the local store bootstrap seeds: it cannot be created (409) or deleted (403).
- A config that does not match `provider` is a `validation-error`, and a mask sent back after the origin changed is refused: send the real password.
- The invalidate tool and the update/delete eviction reach only the process that runs them: other server processes keep their cached adapter until it is evicted there or the process restarts.

## Related
[embedding providers](providers/embedding), [knowledge search](knowledge/search).
