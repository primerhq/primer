# Knowledge

## 1. Purpose

The knowledge subsystem is how the platform stores reference material and, optionally, makes it searchable by meaning. A `Collection` is a wiki: a tree of pure-text documents addressed by slug paths. It is useful the moment a document lands in it, because reading, listing, and grep need no embedder, no vector store, and no indexing pass. Semantic search is an opt-in block on the collection, enabled deliberately and reported honestly when it fails. A second, system-owned collection describes the platform to its own agents: the agents, graphs, tools and collections that exist, plus the shipped agent-facing docs, regenerated from live state on every boot.

## 2. Conceptual model

A `Document` is a node in one collection's tree. The entity row (`primer/model/collection.py`) carries `collection_id`, an optional `parent_id` (absent at the root), a `slug` segment, a derived `path` mirror of the slug chain, an optional `title` defaulting to the slug, and a `meta` bag. The body is not on the row: it lives in the content store as a `ContentRow` keyed by `(collection_id, path)`, which is the single body location. There is no second place a body can hide.

`DocumentTreeService` (`primer/knowledge/tree.py`) is the write chokepoint. Every mutation writes the entity row and the content row inside one `StorageProvider.transaction()`, so the two never diverge. Paths are derived from the parent chain rather than supplied, which is why a move rewrites a whole subtree's paths in one transaction while keeping document ids stable: links and indexed chunks survive a reorganisation.

A `CollectionSearchConfig` on the collection turns semantic search on. It names the embedder, the vector store, an optional cross encoder, the chunk sizing, and a `state` of `indexing`, `ready`, or `error` with a message. `None` means grep-only, which is the default.

```mermaid
erDiagram
    Collection ||--o{ Document : "holds"
    Document ||--o| Document : "parent of"
    Document ||--|| ContentRow : "body in content store"
    Collection ||--o| CollectionSearchConfig : "optional search"
    Document ||--o{ EmbeddingRecord : "chunks when search is on"
```

## 3. Architecture patterns implemented

Transactional pairing: entity row and content row commit or roll back together, so a torn write cannot leave an addressable document with no body.

Derived state: `path` mirrors the parent chain and chunk metadata mirrors `path`. Both are rebuilt rather than edited, which is why a move rewrites metadata instead of re-embedding.

Explicit lifecycle over ambient work: indexing happens when an operator enables search, not on every boot. A failure leaves a partial index and an error state rather than retrying invisibly.

Degrade with a pointer: a surface that cannot serve a request says what will. Semantic search on a grep-only collection returns a conflict naming `grep_collection`; a document read that misses names the siblings it did find.

## 4. Code layout

| Path | Responsibility |
| --- | --- |
| `primer/model/collection.py` | `Collection`, `CollectionSearchConfig`, `ChunkingConfig`, `Document`. |
| `primer/knowledge/tree.py` | `DocumentTreeService`: create, read, update, tree walk, move, recursive delete. |
| `primer/knowledge/grep.py` | `grep_collection`: regex line scan with a cap and a truncated flag. |
| `primer/knowledge/splitter.py` | `split_text`: markdown heading-aware chunking with breadcrumbs. |
| `primer/knowledge/indexing.py` | `index_document`, `remove_document_index`, `rewrite_document_path_meta`. |
| `primer/knowledge/lifecycle.py` | `enable_search`, `disable_search`, `search_status`, `validate_search_config`. |
| `primer/knowledge/importer.py` | `import_zip`: archive directories mapped onto the tree. |
| `primer/knowledge/system_collection.py` | `regenerate_system_collection`: the platform's self-description. |
| `primer/api/routers/knowledge.py` | Collection CRUD plus the docs, grep, import and search-lifecycle routes. |
| `primer/toolset/collections.py` | The always-on `collections` toolset agents navigate with. |

## 5. Data model

`Collection` carries `description`, an optional `search` block, `system`, and `harness_id`. A system collection is read-only through every user-facing path, and the flag itself is the platform's: a create that sets `system` and an update that changes it (a body that leaves it out counts, the default is false) are refused, 403 over REST and `type=forbidden` from `create_collection` / `update_collection`, through one function (`check_collection_system_flag`, `primer/knowledge/checks.py`, over the `forbidden` kind of `EntityCheckError`). The platform writes its system collections straight to storage and is not affected; an update that leaves the flag as it was still works. Without this a client cleared the flag with a PUT and then deleted the collection, and the delete cascade emptied it. Pinned by `tests/api/test_collection_system_flag.py` and `tests/toolset/test_system_collection_system_flag.py`.

`Document` carries `collection_id`, `parent_id`, `slug`, `title`, `path`, `meta`, `harness_id`, `created_at`, `updated_at`. The model accepts `[a-z0-9._-]` slugs so transitional and harness-managed rows validate; the API edge enforces the stricter `[a-z0-9-]`, and the system-collection regenerator opts out of that stricter check because entity ids are the thing users search by.

`CollectionSearchConfig` carries `embedder`, `vector_store_provider_id`, an optional `cross_encoder`, a `ChunkingConfig` of `max_chars` and `overlap`, plus `state` and `error`.

## 6. Lifecycle

A collection is created with a description alone and is immediately usable: create documents, read them, grep them.

Enabling search is a deliberate act. `PUT /v1/collections/{id}/search` validates the referenced providers first, so an unregistered embedder or vector store comes back as a conflict naming the id and where to register it. The backfill then runs inline, because collections are text-only and bounded, and the caller learns the outcome from the response rather than polling. A failure leaves the partial index in place and the state in `error` with the message attached; re-issuing the same request retries.

Disabling always succeeds, even when the provider or namespace is already gone, so a collection can never get stuck enabled.

Writes keep the index honest without re-embedding more than they must. A create or update indexes the new body; a delete unindexes every removed document; a move rewrites each chunk's path metadata, because a rename changes no vectors.

## 7. Persistence

Entity rows go through the normal `Storage` interface. Bodies live in the content store, which enforces `UNIQUE(collection_id, path)`; that uniqueness is what makes sibling-slug collisions a clean conflict rather than a silent overwrite. Vectors live in the collection's configured `SemanticSearchProvider` namespace, keyed by `(collection_id, document_id, chunk_id)` where `chunk_id` is `str(index)` and nothing else.

## 8. Public surfaces

| Surface | Purpose |
| --- | --- |
| `GET /v1/collections/{id}/docs?path=` | Read one document with its children. |
| `GET /v1/collections/{id}/docs?parent=&depth=` | Walk the tree under a parent. |
| `POST /v1/collections/{id}/docs` | Create a node under a parent. |
| `PATCH /v1/collections/{id}/docs?path=` | Update body and/or title. |
| `POST /v1/collections/{id}/docs/move` | Move a node and its subtree. |
| `DELETE /v1/collections/{id}/docs?path=&recursive=` | Delete, optionally recursively. |
| `GET /v1/collections/{id}/grep?q=&path_prefix=` | Regex line search over bodies. |
| `POST /v1/collections/{id}/import` | Import a zip archive as a tree. Zip-bomb caps (FS-04) answer 413 `payload-too-large`: the upload is copied in chunks to a spool and refused past `MAX_ARCHIVE_BYTES` (32 MiB) while it is read; an archive listing more than `MAX_ENTRIES` (10,000) entries (read first from the end records before `zipfile` parses the central directory: the zip64 end record whenever its locator is present, as zipfile does, and the larger of the stated count and `size_cd // 46`, because zipfile walks the directory by its size and never reads the forgeable count fields, while every central header is at least 46 bytes; an unreadable end record is left to zipfile and the parsed count is checked after) or declaring more than `MAX_UNCOMPRESSED_BYTES` (128 MiB) in total is refused before anything is written; each entry is inflated through a bounded read that counts the bytes actually decompressed against the same total, and an entry whose sizes or CRC do not match its headers is a 400. Entry names whose segments slugify to nothing (`..`, `.`) are rejected per entry, so a traversal name cannot climb above `parent`. The app-wide 32 MiB body limit sits in front of the route, so a multipart upload whose archive is just under 32 MiB is refused by the middleware (its own 413, the same type) once the multipart framing pushes the body over the cap. |
| `PUT/GET/DELETE /v1/collections/{id}/search` | Enable, report, disable semantic search. |
| `collections` toolset | `collections_list`, `collection_tree`, `read_document`, `grep_collection`, `semantic_search`, and the write tools. |

Writes to a system collection answer 403 on every one of these.

**Deleting a collection deletes what it owns (ticket 01a1131f "F").** `DELETE /v1/collections/{id}` and the system `delete_collection` tool used to remove only the collection row: its documents, their content rows (`UNIQUE(collection_id, path)`) and its vector chunks stayed, the chunks stayed searchable, and a collection created later under the same id found them waiting. Both now call one function, `purge_collection` (`primer/knowledge/lifecycle.py`), BEFORE the row goes: the REST router through `on_pre_delete` (after the managed-by guard and the reference checks, so a refused delete has emptied nothing) and the tool factory through `pre_delete`. The row is the last thing to go, so a failure part-way leaves a collection that deleting again finishes. Order: (1) the vector namespace, one `drop_collection` whatever the size; (2) the documents with their content rows, 200 at a time, one transaction per batch (a document someone else deleted in the meantime counts as deleted, not a 404), with a guard that raises a plain `PrimerError` (500, `type=storage-error`; not a `ProviderError`, which would read as a 502 blaming the vector store) if a batch makes no progress; (3) the namespace AGAIN and a last look at the documents, because a write that was in flight when (1) ran (the indexing pass of a PUT) recreates the namespace and puts chunks after it, and a document can be created while (2) runs; this repeats for up to three rounds, and a writer that outlasts them gets a `ConflictError` (409, `type=conflict`) with the row left whole; (4) one `DELETE ... WHERE collection_id =` on the content store (`DocumentContentStore.delete_collection`) for content rows that have no document entity, which would otherwise keep a path of the reused id taken; then the row. It is synchronous on purpose: collections are text-only and bounded (a document body is capped at 1 MiB), the vector side is one call, and the caller learns the outcome from the response, as the search backfill (`PUT .../search`) does. Step (1) FAILS CLOSED, unlike `disable_search` (which always succeeds): only "nothing to drop" is tolerated (no search configured, no registry in this process, or `NotFoundError` from a provider row or namespace that is gone); any other failure is a `ProviderError` (502 over REST, `type=provider-error` from the tool) that changes nothing and says how to get unstuck, because chunks left behind would resurface under a reused id. To delete a collection whose vector store is permanently broken, `DELETE .../search` first (it always works), then delete. After a failure part-way the vector namespace is already gone, so search on the collection finds nothing until the delete is retried (or the search is re-enabled, which backfills); the batches that committed are gone and the rest, with the row, are intact, and deleting again finishes. One window stays open and is declared: a write that lands between step (3)'s last look and the row delete would create a document under an id that is then free. Closing it needs a tombstone state on the collection that the write paths refuse while it is set (ticket 01a11a68-4d28). The route documents the answers it can give (403, 409, 502; and 403 on create and update for the flag) through `make_crud_router`'s `extra_create_responses` / `extra_update_responses` / `extra_delete_responses`. A system collection cannot be deleted at all (403 over REST, `type=forbidden` from the tool, the same message the document routes use): the generic delete used to allow it, against what these docs promised. `delete_collection` is declared NOT interruptible (it is several durable steps on two stores). Pinned by `tests/api/test_collection_delete_cascades.py`, `tests/toolset/test_system_collection_delete.py` and the content-store contract (`tests/storage/test_content_store_contract.py`).

## 9. Internal contracts

The content store is the only body location. An entity row without a content row is not a document: it is neither served nor listed.

**Both document surfaces reach the vector store on delete and move (ticket 01a1131f).** `DocumentTreeService` (the `/docs` routes and the collections toolset) always took a best-effort unindexer and a chunk path rewriter; `DocumentService` (the path-addressed `/documents?path=` routes and the system toolset's `put_document` / `get_document_content` / `list_documents` / `move_document`) took only an indexer, so deleting a document by path left its chunks searchable and a move left every chunk's `meta.path` at the old path. `DocumentService.delete` now calls the unindexer, and `move` the path rewriter, AFTER their transaction (and, for a delete, the event): the rows are the truth, so the hooks are best-effort and log and swallow their own failures, and nothing re-embeds on a move (metadata only). They are the shared `make_document_unindexer` / `make_document_path_rewriter` of `primer/knowledge/indexing.py`, passed by every place that builds the service: the REST dependency `get_document_service` and the toolset's `_document_service_factory` (when a semantic-search registry is wired). A service built with no hooks (search off, unit tests) deletes and moves exactly as before. Pinned by `tests/api/test_document_path_service_unindexes.py` and `tests/toolset/test_system_document_service_unindexes.py`.

`chunk_id` is `str(index)`. No other shape exists.

A miss carries alternatives. `DocumentTreeService.resolve` raises with the siblings of the parent it searched, and the toolset passes that message through, so a wrong guess teaches the right path.

The regenerator writes through the service directly rather than the API, because the 403 guards users, not the platform describing itself.

## 10. Testing patterns

Tree behaviour is tested against a real sqlite provider rather than a fake, because the transactional pairing and the `UNIQUE` constraint are the things under test.

`tests/knowledge/test_delete_unindexes.py` pins the three defects the v2 model was built to close: a delete removes its vectors, a delete removes its content row, and only one chunk-id shape exists. They pass against the implementation, which is the point: they fail loudly if a refactor reintroduces the orphaning.

`tests/docs/test_s2_grep_clean.py` pins the deletions, so a removed surface cannot creep back by import.

## 11. Historical decisions

Bodies once lived in `Document.meta` and in the vector table, and a migration copied them into the content store. v2 made the content store the single location by clean break: the migration is retired to a no-op that keeps its version slot.

Binary document conversion is gone. The loader, splitter and ingester that turned PDFs into chunks were removed along with the optional extra that powered them; a collection holds text, and converting a binary is a job for a tool run before the file reaches primer.

MMR was removed when the collection model changed shape: its config became unreachable from any live model, and an unreachable-but-tested code path is worse than a deleted one.

The five reserved `_internal_*` collections and their search toolset were replaced by one system collection reachable through the ordinary collections tools. Indexing the platform's own entities no longer requires a separate subsystem, and the toggle that used to gate the whole thing now governs vectorisation only.
