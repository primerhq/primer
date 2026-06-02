---
slug: knowledge
title: Knowledge — collections and documents
summary: How to organise reference material in primer using Collections and Documents, including the ingest pipeline and how to read content back.
related: [semantic-search, agents]
mcp_tools:
  - system::list_collections
  - system::get_collection
  - system::create_collection
  - system::update_collection
  - system::delete_collection
  - system::find_collections
  - system::list_documents
  - system::get_document
  - system::create_document
  - system::update_document
  - system::delete_document
  - system::find_documents
  - system::get_document_content
  - system::put_document
  - system::list_collection_documents
  - system::find_collection_documents_by_meta
---

# Knowledge — collections and documents

## Overview

Knowledge in primer is two nested entities. A **Collection** is a
named container with an `id`, a `description`, a binding to a
configured `SemanticSearchProvider` (the vector store), and a
binding to a configured embedder (so the collection has stable
embedding semantics). A **Document** belongs to a Collection by
`collection_id`, has its own `id`, `name`, and a `meta` bag for
arbitrary metadata, and carries text content under `meta['content']`
(by convention — the storage backend doesn't enforce a schema for
this).

The Collections + Documents model is the primer-provided answer to
"give my agent access to a body of reference material." Operators
create one Collection per knowledge domain — engineering runbooks,
support FAQs, product documentation — and POST Documents into it.
Agents discover collections via `search::search_collections`
(metadata search), iterate documents via
`system::list_collection_documents`, and fetch text via
`system::get_document_content`.

The piece that's still in flight is the **live ingest** path: the
multipart-upload endpoint that takes a PDF/DOCX/MD file, runs it
through [primer/ingest/](../../primer/ingest/)'s `DocumentIngester`
(load → split → embed → store), and produces both a Document row and
multi-chunk vector records. That endpoint is partially built but
not yet wired into the REST surface. Today, `POST /v1/documents`
persists the Document row only — no chunking, no vectorising — and
`system::search_collection` is a stub that returns
`type=not-implemented`. The retrieval path that DOES work is reading
content by id, which is what `system::get_document_content` gives you.

A different special-purpose corner of the knowledge surface is the
**internal collections** — five reserved collections owned by
primer itself, including `_internal_ai_docs` (the docs you're
reading). Those are documented separately at
[semantic-search](semantic-search.md). The CRUD tools listed here
work uniformly across user collections and the internal ones, but
the internal ones are managed by the IC subsystem (their CDC keeps
them in sync) and shouldn't be edited via these tools.

## Mental model

A `Collection`:
- `id` — operator-chosen identifier; immutable.
- `description` — free text. This is what
  `search::search_collections` embeds.
- `embedder` — `{provider_id, model}`. Bound at create time; not
  changeable (changing the embedder would invalidate every existing
  chunk's vector dimensions).
- `search_provider_id` — id of a `SemanticSearchProvider` row. Bound
  at create time; immutable for the same reason as `embedder`.
- `system` — true for the reserved internal collections; user
  collections are created with `system=false`. The CRUD layer
  refuses to delete system collections.

A `Document`:
- `id` — operator-chosen identifier; unique within the collection.
- `collection_id` — the owning Collection.
- `name` — human-readable label.
- `meta` — arbitrary JSON. By convention `meta['content']` holds the
  raw text. `meta['content_hash']` (when present) is a sha256 over
  the content used for change detection.
- `harness_id` — null for user-created documents. Non-null for
  documents managed by a harness install; mutation via the public
  CRUD endpoints returns 409 — use the harness's sync/uninstall.

A typical use case ties a Collection to an agent: the agent's
`system_prompt` references the collection by id, and the agent's
tools include `system::list_collection_documents` +
`system::get_document_content` so it can iterate and read. With
semantic search available, the agent first calls
`search::search_collections` to locate the right collection by
description, then `system::list_collection_documents` to enumerate.

## Lifecycle and states

There's no state machine on a Collection or Document — they're
plain CRUD. The interesting lifecycle is the **ingest path**, which
in v1 is fully implemented in code but only partially exposed
through HTTP. The pipeline:

1. **Load.** `DocumentLoader.load(source)` reads bytes / a Path /
   a URL string and produces a `LoadedDocument` (text + structural
   metadata). The default `DoclingLoader` handles PDF, DOCX, PPTX,
   HTML, plain text, markdown.
2. **Split.** `DocumentSplitter.split(loaded)` produces a list of
   `Chunk` objects with positions and per-chunk metadata. Default
   is the `DoclingSplitter` (structure-aware — respects headings,
   tables, code blocks). `RecursiveSplitter` is the pure-Python
   fallback for environments where docling isn't desired.
3. **Embed.** First chunk embedded alone to learn vector
   dimensionality; remaining chunks embedded in batches of 32.
4. **Store.** Lazy-creates the vector-store collection with the
   probed dim, inserts one record per chunk.

`DocumentIngester` is the orchestrator; constructor takes the
Collection, an Embedder, a VectorStore, plus optional loader and
splitter. The internal-collections subsystem uses it directly for
`_internal_ai_docs`. End-user code does not — the live REST entry
point that calls `DocumentIngester` from a multipart upload isn't
yet wired up. So the practical state in v1:

- **Live and working.** Collection CRUD, Document CRUD,
  `get_document_content`, `put_document` (creates a Document with
  text under `meta['content']`; no chunking/embedding).
- **Stubbed.** `search_collection` returns `type=not-implemented`.
- **Internal use only.** `DocumentIngester` is fully implemented
  but only the IC subsystem calls it.

## MCP tools

Tools group naturally into three sets: collection CRUD, document
CRUD, and the extra "use-this-not-that" tools.

### Collection CRUD

- `system::list_collections` — paginated listing.
- `system::get_collection` — fetch by id. 404 on miss.
- `system::create_collection` — body needs `id`, `description`,
  `embedder`, `search_provider_id`. Reject reserved ids
  (`_internal_*`).
- `system::update_collection` — partial update. `embedder` and
  `search_provider_id` are not editable post-create — attempting
  triggers 422.
- `system::delete_collection` — cascade-blocked if any document
  references it. Reject reserved ids.
- `system::find_collections` — predicate-based query.

### Document CRUD

- `system::list_documents` — paginated listing across all
  collections.
- `system::get_document` — fetch the row by id. Does NOT return
  content — content lives in `meta['content']` and is best fetched
  via the dedicated tool.
- `system::create_document` — needs `collection_id`, `name`,
  optionally `meta`. Creates the row without chunking.
- `system::update_document` — partial update of `name` and `meta`.
- `system::delete_document` — removes the row + any vector records
  bound by `document_id` in the store.
- `system::find_documents` — predicate-based query.

### Extras

- `system::list_collection_documents` — same as
  `list_documents(collection_id=X)` but more direct; first-class
  pagination on the collection.
- `system::find_collection_documents_by_meta` — filter on
  `meta.<key> == <value>`. Returns matching documents. Use this for
  faceted retrieval (`meta.kind == "runbook"`).
- `system::get_document_content` — pulls text from
  `meta['content']`. Returns `{id, collection_id, name, content}`.
  Empty string if no content stored.
- `system::put_document` — upsert by id with raw text. Stores
  content under `meta['content']`. **Does NOT vectorise.** Idempotent.

The two stubs to know about:

- `system::search_collection` — returns
  `is_error=true type=not-implemented` until the user-doc ingest
  pipeline lands.
- `system::refresh_collection` — same. The IC subsystem's
  re-bootstrap covers refresh for the five internal collections;
  user collections will get their own refresh once chunking is
  wired through.

## Workflows

### Workflow 1 — load a runbook collection and read a doc by id

**Goal.** Operator has created a `runbooks` collection and POSTed
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

2. List documents in that collection:

```json
{
  "tool": "system::list_collection_documents",
  "arguments": {"collection_id": "runbooks", "limit": 100}
}
```

Returns items like `{"id": "db-failover", "name": "Database failover", "meta": {"content_hash": "...", "tag": "ops"}}`.

3. Fetch the content of the relevant one:

```json
{
  "tool": "system::get_document_content",
  "arguments": {"id": "db-failover"}
}
```

Returns `{"id": "db-failover", "collection_id": "runbooks", "name": "Database failover", "content": "## Symptoms\\n\\n..."}`.

### Workflow 2 — upsert a document programmatically

**Goal.** Agent has been asked to remember a piece of information
across sessions. It writes it into a "memory" collection.

Assume the operator has created the `agent-memory` collection in
advance.

```json
{
  "tool": "system::put_document",
  "arguments": {
    "id": "user-preferences-2026-06-03",
    "collection_id": "agent-memory",
    "name": "User preferences as of 2026-06-03",
    "content": "User prefers concise outputs and metric units."
  }
}
```

Returns the Document row. Subsequent reads via
`system::get_document_content` see the updated content. Note: the
content is NOT searchable via `search_collection` until that tool
ships; the agent has to know the document id to read it back.

## Gotchas

- **`POST /v1/documents` does not vectorise.** The endpoint
  persists the Document row; chunking + embedding is the deferred
  multipart-upload endpoint. Don't expect to ingest a Document and
  then immediately `search_collection` for it.
- **`system::search_collection` is currently a stub.** Returns
  `type=not-implemented`. Use `system::find_collection_documents_by_meta`
  or `system::get_document_content` directly for retrieval until
  the live ingest pipeline ships.
- **Embedder and search_provider are immutable on a Collection.**
  Changing them would invalidate every existing vector record's
  dimensionality. To "switch embedder", create a new Collection and
  re-ingest.
- **Reserved ids start with `_internal_`.** `system::create_collection`
  rejects them with 422. The five internal collections are managed
  by the IC subsystem; don't try to CRUD them via these tools.
- **`harness_id` makes a Document immutable through CRUD.** Document
  rows installed by a harness are tagged with `harness_id`; PUT and
  DELETE return 409. Use harness sync/uninstall instead.
- **Content lives in `meta['content']` by convention.** Future
  versions may move it to a dedicated field. Code that reaches
  directly into `meta['content']` will need to migrate; using
  `system::get_document_content` is the forward-compatible path.
- **Delete cascade.** Deleting a Collection with any Documents
  inside returns 409. Either delete the Documents first or use the
  Collection delete endpoint's cascade flag (operator-only; not
  exposed as a tool).

## Related

- [semantic-search](semantic-search.md) — the internal collections
  (`_internal_*`) and the `search::*` toolset.
- [agents](agents.md) — agents typically bind one or more
  collections via their `system_prompt` or via tool calls in their
  prompt template.
