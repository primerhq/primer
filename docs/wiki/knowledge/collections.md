---
title: "Collections"
summary: "A collection is a named tree of documents with optional search: create, change, find, refresh and delete collections."
order: 1
tools: [system__list_collections, system__get_collection, system__create_collection, system__update_collection, system__delete_collection, system__find_collections, system__refresh_collection]
related: [knowledge/documents, knowledge/search, providers/semantic-search]
---
## What it is
A collection is a named tree of documents, optionally searchable by meaning. This page covers the `system` collection tools: create a row, read it back, change it, find it, refresh its index and delete it. Write documents on the row at [write documents](knowledge/documents), read them at [navigate and read documents](knowledge/navigation), and search the row at [search a collection](knowledge/search).

## Concepts
- The fields: `id` (when omitted the server assigns `collection-<hex>`; a sent id must match `[a-z0-9._-]+` and is immutable), `description` (free text; the collection's page in the live map carries it, so it is what a search for the collection matches), `search` (the optional search block), `system` (platform-owned) and `harness_id` (the harness that manages the row).
- A collection is grep-only until its `search` block is set. Semantic search needs an embedding provider and a vector store: an admin registers them at [embedding providers](providers/embedding) and [semantic-search providers](providers/semantic-search). The block names `embedder` (provider and model), `vector_store_provider_id`, an optional `cross_encoder` that reranks the vector hits, and `chunking` (`max_chars` and `overlap` for the heading-aware splitter).
- The block's `state` is `ready`, `indexing` or `error`, with an `error` hint when it is `error`. Both fields are lifecycle-owned and never client-set: turning search on starts `indexing` and ends `ready`, or leaves `error` with the partial index intact.
- What can change after create: everything in the block except `state` and `error`. Re-PUT `PUT /v1/collections/{collection_id}/search` to swap the `embedder`, the vector store, the `cross_encoder` or the `chunking`: the route validates the providers and backfills what is already there. Disable with `DELETE /v1/collections/{collection_id}/search`: the vectors drop and the collection goes grep-only. Both routes are for the collection's owners. Replacing the whole row (`system__update_collection`) never changes the search block: send the stored block back unchanged, or the update is refused with `type=forbidden`.
- The platform's own ids are reserved. A create under an id that starts with `_internal_` (the four internal collections, one per entity kind: agent, graph, collection and tool) or the platform wiki's id `system` is refused with 422 `collection_id_reserved` (REST) or `type=validation-error` (the tool), and nothing is stored. An update cannot plant one, because ids are immutable.
- The `system` flag marks a collection the platform regenerates (the wiki is one). A create that sets it and an update that changes it are refused (403 over REST, `type=forbidden` from the tools); a body that omits it counts as clearing it, so read the row and send the stored value back. A system collection's documents cannot be written and the collection cannot be deleted through any user path.
- A row with `harness_id` set is managed by that harness; the CRUD layer refuses user writes on it with `type=conflict`.
- A collection is owned and shared like an agent ([sharing and access](start-here/sharing-and-access)), and its documents share its access: `read` reads, greps and searches them, `full` writes, moves and deletes them, `owner` deletes the collection, shares it and turns search on or off. A collection you cannot read answers as if it did not exist. The platform wiki (`system`) is public and read-only; the `_internal_` collections are admin-only.

## How to
1. See what exists with `system__list_collections`, or narrow it with `system__find_collections`.
2. Create the row with `system__create_collection`; it is grep-only until search is on.
3. Turn search on: an admin registers the providers first, then the search block goes on over `PUT /v1/collections/{collection_id}/search`, which backfills the existing documents inline.
4. Read the row back with `system__get_collection`; `state` is `ready` when the backfill finished.

## Tool calls
### List the collections
`system__list_collections` pages the Collection rows.

```json
{"toolset_id": "system", "tool_name": "list_collections", "arguments": {"offset": 0, "limit": 20}}
```

Reference: [`system__list_collections`](reference/tools/system/list_collections)

### Read one collection
`system__get_collection` fetches a row by id and returns the full entity.

```json
{"toolset_id": "system", "tool_name": "get_collection", "arguments": {"id": "kb-1"}}
```

Reference: [`system__get_collection`](reference/tools/system/get_collection)

### Create a collection
`system__create_collection` stores a new row from the full `entity` body; the `id` is optional and must be unique.

```json
{"toolset_id": "system", "tool_name": "create_collection", "arguments": {"entity": {"id": "kb-1", "description": "Engineering on-call runbooks"}}}
```

Reference: [`system__create_collection`](reference/tools/system/create_collection)

### Update a collection
`system__update_collection` replaces the whole row; send the `id` and the full entity you read first.

```json
{"toolset_id": "system", "tool_name": "update_collection", "arguments": {"id": "kb-1", "entity": {"id": "kb-1", "description": "Engineering on-call runbooks"}}}
```

Reference: [`system__update_collection`](reference/tools/system/update_collection)

### Delete a collection
`system__delete_collection` removes the row, its documents, their content and its vector chunks.

```json
{"toolset_id": "system", "tool_name": "delete_collection", "arguments": {"id": "kb-1"}}
```

Reference: [`system__delete_collection`](reference/tools/system/delete_collection)

### Find collections by field
`system__find_collections` matches rows by a predicate tree; a `predicate` of null matches all rows.

```json
{"toolset_id": "system", "tool_name": "find_collections", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "kb-1"}}}}
```

Reference: [`system__find_collections`](reference/tools/system/find_collections)

### Refresh a collection
`system__refresh_collection` re-embeds every document; today a stub that answers `type=not-implemented` until the SearchService ingestion pipeline lands.

```json
{"toolset_id": "system", "tool_name": "refresh_collection", "arguments": {"collection_id": "kb-1"}}
```

Reference: [`system__refresh_collection`](reference/tools/system/refresh_collection)

## Gotchas
- Delete refuses a system collection (`type=forbidden`) and a harness-managed row (`type=conflict`), and it is refused with `type=provider-error` when the vector store cannot be reached: disable the collection's search first, then delete. There is no cascade flag: the cascade is always on.
- An embedder whose vector size differs from the stored one is refused with 422 `dimension-mismatch` (it names both dimensions) before any embedding work: the write answers the error although the body is already stored. The fix is to re-PUT the search block with a matching embedder, or to keep a separate collection for the new model.
- A secret-bearing field cannot be a predicate key: a `find_` or `list_` that sorts or filters on it is refused before it queries.
- Reserved ids (the `_internal_` prefix and `system`) are refused on create, and the internal collections and the wiki are managed by the platform: read them, do not CRUD them.

## Related
[Write documents](knowledge/documents), [Search a collection](knowledge/search), [Semantic-search providers](providers/semantic-search).
