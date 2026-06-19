---
slug: knowledge
title: Knowledge - collections and documents
summary: How to organise reference material in primer using path-addressed Collections and Documents, including the content store, the document tools, and how to read content back.
related: [semantic-search, agents]
mcp_tools:
  - system::list_collections
  - system::get_collection
  - system::create_collection
  - system::update_collection
  - system::delete_collection
  - system::find_collections
  - system::get_document_content
  - system::put_document
  - system::list_documents
  - system::move_document
  - system::list_collection_documents
  - system::find_collection_documents_by_meta
  - system::search_collection
---

# Knowledge - collections and documents

## Overview

Knowledge in primer is two nested entities. A **Collection** is a
named container with an `id`, a `description`, a binding to a
configured `SemanticSearchProvider` (the vector store), and a
binding to a configured embedder (so the collection has stable
embedding semantics). A **Document** belongs to a Collection by
`collection_id` and is addressed by a **path** that is unique within
the collection (for example `runbooks/db-failover.md`). It has an
optional `title` (defaults to the path's last segment), a `meta` bag
for arbitrary metadata, and a body. The body is *not* on the document
row: it lives in a first-class content store keyed by
`(collection_id, path)`, separate from both the entity row and the
vector index.

The Collections + Documents model is the primer-provided answer to
"give my agent access to a body of reference material." Operators
create one Collection per knowledge domain - engineering runbooks,
support FAQs, product documentation - and write Documents into it by
path. Agents discover collections via `search::search_collections`
(metadata search), browse a collection's paths via
`system::list_documents`, and fetch a body via
`system::get_document_content`.

Use `system::find_collection_documents_by_meta` when you want
documents by exact metadata predicate; not when you want a ranked
natural-language match (that is what the `search::search_*` tools in
[semantic-search](semantic-search.md) do). Note
`search::search_collections` ranks collection *metadata*, not the
documents inside them.

Search is **on** in this release. Writing a document with
`system::put_document` both stores its body in the content store and
(re-)indexes its chunks into the collection's vector store, and
`system::search_collection` returns ranked chunk hits over a
collection's contents. The vector index is a derived, optional index
over the body rather than the place the body lives.

A different special-purpose corner of the knowledge surface is the
**internal collections** - five reserved collections owned by
primer itself, including `_internal_ai_docs` (the docs you're
reading). Those are documented separately at
[semantic-search](semantic-search.md). The collection CRUD tools
listed here work uniformly across user collections and the internal
ones, but the internal ones are managed by the IC subsystem (their
CDC keeps them in sync) and shouldn't be edited via these tools.

## Mental model

A `Collection`:
- `id` - optional on create; supply one to use it verbatim, or
  omit it and the server assigns `collection-<hex>`. Immutable
  after creation.
- `description` - free text. This is what
  `search::search_collections` embeds.
- `embedder` - `{provider_id, model}`. Bound at create time; not
  changeable (changing the embedder would invalidate every existing
  chunk's vector dimensions). PUT returns 422 if you attempt to
  change either field.
- `search_provider_id` - id of a `SemanticSearchProvider` row. Bound
  at create time; immutable for the same reason as `embedder`.
- `search` - optional `CollectionSearch` with two sub-fields, both
  independently optional and editable at any time without re-indexing:
  - `mmr` - Maximal Marginal Relevance config: `lambda_mult` (float
    0-1, default 0.5; 1.0 = pure relevance, 0.0 = max diversity) and
    `fetch_k` (int or null; candidates fetched before MMR runs).
  - `cer` - cross-encoder reranker config: `provider_id` (a
    `CrossEncoderProvider` id), `model` (model name on that provider),
    and `top_n` (int, default 100; candidates the reranker scores).
  Set `search: null` to disable all retrieval augmentation and use
  vanilla vector ranking.
- `system` - true for the reserved internal collections; user
  collections are created with `system=false`. The CRUD layer
  refuses to delete system collections.

A `Document`:
- `path` - the document's address within the collection, unique per
  collection (for example `runbooks/db-failover.md`). This is the
  primary key you read, write, and move documents by. Path segments
  cannot be empty, `.`, or `..`, and the path cannot start or end
  with `/`.
- `collection_id` - the owning Collection.
- `title` - human-readable label; defaults to the path's last
  segment when unset.
- `meta` - arbitrary JSON metadata. The body does **not** live here -
  it is stored in the content store keyed by `(collection_id, path)`.
- `harness_id` - null for user-created documents. Non-null for
  documents managed by a harness install.

A typical use case ties a Collection to an agent: the agent's
`system_prompt` references the collection by id, and the agent's
tools include `system::list_documents` +
`system::get_document_content` so it can browse paths and read. With
semantic search available, the agent first calls
`search::search_collections` to locate the right collection by
description, then `system::search_collection` to pull the most
relevant chunks or `system::list_documents` to enumerate paths.

## Lifecycle and states

There's no state machine on a Collection or Document - they're plain
records. The interesting lifecycle is the **write path** behind
`system::put_document`, driven by the `DocumentService`:

1. **Store the body.** The body is written to the content store at
   `(collection_id, path)` in the same transaction as the document
   entity row. Writing to an existing path replaces it.
2. **Index.** When the collection has search on, the new body is
   chunked, each chunk is embedded, and the vectors are upserted into
   the collection's vector store - best-effort, after the body is
   durably stored.

So the practical state in this release:

- **Live and working.** Collection CRUD; path-addressed document
  read/write/list/move (`get_document_content`, `put_document`,
  `list_documents`, `move_document`); per-collection semantic search
  (`search_collection`) over the indexed bodies.
- **Raw row CRUD.** The generic `*_document` CRUD tools still exist
  for row-level access to the document entity, but they do not touch
  the content store - prefer the path-addressed tools for content.

## MCP tools

Tools group naturally into three sets: collection CRUD, the
path-addressed document tools, and the per-collection extras.

### Collection CRUD

- `system::list_collections` - paginated listing.
- `system::get_collection` - fetch by id. 404 on miss.
- `system::create_collection` - body needs `description`,
  `embedder`, `search_provider_id`, and an optional `id`. Omit `id`
  and the server assigns `collection-<hex>` (e.g.
  `collection-3f9a1c8d`); supply one to use it verbatim. Immutable
  after creation. Reject reserved ids (`_internal_*`).
- `system::update_collection` - partial update. `embedder` and
  `search_provider_id` are not editable post-create - attempting
  triggers 422.
- `system::delete_collection` - cascade-blocked if any document
  references it. Reject reserved ids.
- `system::find_collections` - predicate-based query.

### Document tools (path-addressed)

These are the tools to reach for. They go through the
`DocumentService`, so they keep the content store, the entity row,
and the vector index consistent.

- `system::get_document_content` - args `collection_id`, `path`.
  Reads the body from the content store. Returns
  `{id, collection_id, path, title, content}`. Returns
  `type=not-found` if no document lives at that path.
- `system::put_document` - args `collection_id`, `path`, `content`,
  optional `title`, optional `meta`. Writes the body to the content
  store at `(collection_id, path)` and creates or replaces the entity
  at that path. When search is on, re-indexes the document
  best-effort after the write. Returns the stored `Document`.
- `system::list_documents` - args `collection_id`, optional `prefix`.
  Lists the collection's documents by path without loading any body.
  Returns `{documents: [{path, document_id, size}, ...]}`. Pass a
  `prefix` (e.g. `concepts/`) to scope to a subtree.
- `system::move_document` - args `collection_id`, `from`, `to`.
  Changes a document's path, preserving its body, title, and
  metadata. Fails if the `to` path is already taken.

### Per-collection extras

- `system::list_collection_documents` - lists a collection's document
  rows (paginated). Returns entity rows, not bodies; use
  `get_document_content` for a body.
- `system::find_collection_documents_by_meta` - filter on
  `meta.<key> == <value>`. Returns matching documents. Use this for
  faceted retrieval (`meta.kind == "runbook"`).
- `system::search_collection` - runs a semantic search over the
  collection's indexed document contents and returns ranked chunk
  hits (`document_id`, `chunk_id`, `score`, `text`, `meta`), most
  relevant first, using the collection's own embedder and vector
  store.

## Workflows

### Workflow 1 - browse a runbook collection and read a doc by path

**Goal.** Operator has created a `runbooks` collection and written
three runbook documents into it via `put_document`. An agent needs
to find and read the right one.

1. List collections to confirm the runbooks collection exists:

```json
{
  "tool": "system::list_collections",
  "arguments": {"limit": 100}
}
```

Returns items including `{"id": "runbooks", "description": "Engineering on-call runbooks", ...}`.

2. List documents in that collection by path:

```json
{
  "tool": "system::list_documents",
  "arguments": {"collection_id": "runbooks", "prefix": "ops/"}
}
```

Returns `{"documents": [{"path": "ops/db-failover.md", "document_id": "document-...", "size": 412}, ...]}`.

3. Fetch the body of the relevant one by path:

```json
{
  "tool": "system::get_document_content",
  "arguments": {"collection_id": "runbooks", "path": "ops/db-failover.md"}
}
```

Returns `{"id": "document-...", "collection_id": "runbooks", "path": "ops/db-failover.md", "title": "db-failover.md", "content": "## Symptoms\\n\\n..."}`.

### Workflow 2 - write a document programmatically

**Goal.** Agent has been asked to remember a piece of information
across sessions. It writes it into a "memory" collection.

Assume the operator has created the `agent-memory` collection in
advance.

```json
{
  "tool": "system::put_document",
  "arguments": {
    "collection_id": "agent-memory",
    "path": "preferences/2026-06-03.md",
    "content": "User prefers concise outputs and metric units.",
    "title": "User preferences as of 2026-06-03"
  }
}
```

Returns the stored Document. Subsequent reads via
`system::get_document_content` with the same `(collection_id, path)`
see the updated body. With search on, the new content is also
searchable via `system::search_collection`.

## Gotchas

- **Documents are addressed by path, not id.** Read, write, and move
  with `(collection_id, path)`. Writing to an existing path replaces
  that document - the path is its identity and a natural idempotency
  key.
- **The body lives in the content store, not in `meta`.** Don't write
  content into `meta` and don't expect `get_document_content` to read
  it from there; it reads the content store at `(collection_id, path)`.
- **`put_document` re-indexes; raw CRUD does not.** The path-addressed
  `put_document` keeps the body, the row, and the vector index
  consistent. The generic `create_document` / `update_document` CRUD
  tools operate on the entity row only and do not write the content
  store or index; prefer the path-addressed tools for content.
- **Embedder and search_provider are immutable on a Collection.**
  Changing them would invalidate every existing vector record's
  dimensionality. To "switch embedder", create a new Collection and
  re-ingest. The PUT endpoint enforces this: attempting to change
  `embedder.provider_id`, `embedder.model`, or `search_provider_id`
  returns 422. The `search` field (MMR + cross-encoder config) IS
  mutable at any time without re-indexing.
- **Embedding dimension mismatch returns 422 on the first write.**
  The indexing pipeline probes the active embedder once before
  embedding any chunks. If the embedder's output dimension does not
  match the dimension already stored in the vector store for that
  collection (from a prior write with a different model), the API
  returns HTTP 422 with `type=/errors/dimension-mismatch` and a
  message naming both dimensions. To resolve: delete the documents
  from the collection, drop and re-create the collection with the new
  `embedder` binding, then re-write. Alternatively, create a separate
  collection for the new embedding model.
- **Reserved ids start with `_internal_`.** `system::create_collection`
  rejects them with 422. The five internal collections are managed
  by the IC subsystem; don't try to CRUD them via these tools.
- **`harness_id` makes a Document immutable through CRUD.** Document
  rows installed by a harness are tagged with `harness_id`; mutating
  them through the public CRUD endpoints returns 409. Use the
  harness's sync/uninstall instead.
- **Delete cascade.** Deleting a Collection with any Documents
  inside returns 409. Either delete the Documents first or use the
  Collection delete endpoint's cascade flag (operator-only; not
  exposed as a tool).

## Related

- [semantic-search](semantic-search.md) - user-defined collections and
  the internal `_internal_*` collections + `search::*` toolset.
- [agents](agents.md) - agents typically bind one or more
  collections via their `system_prompt` or via tool calls in their
  prompt template.
