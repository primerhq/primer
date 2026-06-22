---
slug: semantic-search
title: Semantic search - internal collections and discovery
summary: How primer's internal-collections subsystem indexes agents, graphs, tools, collections, and platform docs for vector retrieval via the search:: toolset.
related: [knowledge, agents, graphs, mcp-exposure]
mcp_tools:
  - search::search_agents
  - search::search_graphs
  - search::search_collections
  - search::search_tools
  - search::search_ai_docs
---

# Semantic search - internal collections and discovery

## Overview

Primer has two distinct semantic-search surfaces that agents often
confuse. The first is user-defined knowledge: a `Collection` row that
an operator created with `system::create_collection`, into which
documents have been written by path. That surface uses
`system::list_documents`, `system::get_document_content`, and
`system::search_collection`. The second is **internal collections** -
five reserved collections (id-prefixed with `_internal_`) that primer
itself maintains, automatically, so agents can discover what primer
contains via natural-language queries. This doc is about the second.

The five internal collections are: `_internal_agents` (one vector per
configured agent row), `_internal_graphs` (one per graph),
`_internal_collections` (one per user collection - the metadata
row, not the documents), `_internal_tools` (one per tool descriptor
across every reachable toolset), and `_internal_ai_docs` (multi-chunk
embeddings of the very docs you're reading). Each is search-only
from the agent side - write paths are operator-only (via UI/REST for
the first four; via a code-shipped markdown bundle for the fifth).

The point is discoverability. An agent connected to primer for the
first time can ask "do you have an agent that summarises PDFs?" and
get back the relevant `Agent` ids. It can ask "what tool fetches the
weather?" and get back the matching scoped tool ids. It can ask
"how do triggers work?" and get back the specific subsection of
[triggers-and-subscriptions](triggers-and-subscriptions.md) that
answers. All five searches share the same backing infrastructure:
the same embedder, the same vector store, the same per-query
embedding path.

Use a `search::search_*` tool when you have a natural-language
query and want ranked matches; not when you already know the exact
id or want an exact metadata predicate (use the `system::find_*` /
`system::get_*` CRUD tools in [knowledge](knowledge.md) instead).

## Mental model

The **internal-collections subsystem** is the runtime object that
owns these collections. It's instantiated at app startup from the
`InternalCollectionsConfig` singleton row - which carries the
embedding provider id, the embedding model name, the semantic-search
provider id (the vector store), and optional rerank/MMR config. The
subsystem is opt-in: a fresh primer install has no config row, no
subsystem, no `search::*` tools available. An operator activates the
subsystem from the console (or by POSTing the config row + calling
`POST /v1/internal_collections/bootstrap`).

Once activated, the subsystem does two things:

1. **Bootstrap.** Materialises the five collection rows, probes
   embedding dimensionality, creates the vector-store backings,
   then walks the existing entity rows (agents, graphs, user
   collections, every toolset's tools, and every markdown file in
   the agent-docs directory, which resolves to `docs/agents/` by
   default) and embeds them. The first four collections
   store one vector per entity. The fifth - `_internal_ai_docs` -
   uses [primer/ingest/](../../primer/ingest/)'s `DocumentIngester`
   to chunk each markdown file by section and embed each chunk
   separately. Search against the docs collection therefore returns
   specific subsections, not whole files.
2. **CDC.** Subscribes to entity mutation hooks for the first four
   types. When an agent is created/updated/deleted, an `IngestEvent`
   is enqueued; a background worker dequeues and applies. So the
   indexes are eventually consistent with storage. The docs
   collection has no CDC path - it's only refreshed on bootstrap or
   re-bootstrap, by content-hash skip.

A search call goes:
- query string → embedder (`task_type="retrieval_query"`) → query vector
- vector → vector store → top-k hits
- hits returned as `{document_id, chunk_id, score, text, meta}` per hit

For the first four collections, `document_id` is the entity id
(`ag-foo`, `gr-bar`, `col-baz`, `system::list_agents`). For the docs
collection, `document_id` is the slug of the markdown file
(`agents`, `chats`, etc.). A docs hit carries the matched subsection
as its `text`; the internal AI-docs bodies are not stored in the
user-document content store, so the slug is an index identifier for
ranking and dedup, not a `(collection_id, path)` you can read back
with `system::get_document_content`.

## Lifecycle and states

The subsystem has three observable states:

- **not configured** - no `InternalCollectionsConfig` row exists.
  The `search::*` tools are absent from `tools/list`.
  `subsystem.search()` raises `subsystem-inactive`.
- **configured, not bootstrapped** - the config row exists but
  `activated_at` is null. The `search::*` tools appear (the toolset
  is mounted) but every call returns `is_error=true` with
  `type=subsystem-inactive` until bootstrap completes.
- **active** - `activated_at` is set; bootstrap succeeded. Searches
  work. The CDC worker is running.

The bootstrap is a multi-phase process the UI observes via a
`InternalCollectionsBootstrapStatus` singleton row. Phases:
`drain_queue → materialise_collections → ingest_agents → ingest_graphs
→ ingest_collections → ingest_tools → ingest_ai_docs → finalize`. The
phase + per-phase counters are persisted so a long bootstrap can be
watched across browser refreshes / page navigation. Re-bootstrap is
safe - collections are re-materialised idempotently and per-entity
records are upserted by id.

The `_internal_ai_docs` collection has its own micro-lifecycle: on
each bootstrap run, every `*.md` file under the agent-docs directory
(`docs/agents/` by default) is hashed (sha256). The hash is compared to the existing
`Document.meta['content_hash']` - equal hashes mean "no change, skip
re-embedding"; different hashes mean "re-ingest with replace=True"
which drops prior chunks and re-embeds. New files create new
Documents. Files removed from disk leave their old Documents in
storage (no GC in v1 - restart with `--reset-ai-docs` if you need
clean state, or delete via `system::delete_document`).

## MCP tools

The `search` reserved toolset exposes one tool per collection. All
five share the same input shape:

```json
{
  "query": "<free-text query>",
  "top_k": 10
}
```

`top_k` is bounded `[1, 100]`, default 10. `query` is bounded
`min_length=1`.

### `search::search_agents`

**Purpose.** Find configured `Agent` rows by description or system
prompt. Returns the agent id and the embedded text (description +
system_prompt joined).

**Returns.** `{"hits": [{"document_id": "<agent_id>", "chunk_id":
"0", "score": <float>, "text": "<embedded>", "meta": {"entity_type":
"agent", ... <agent row fields> ...}}, ...]}`.

**Errors.** `is_error=true` with `type=subsystem-inactive` if not
bootstrapped. `validation-error` on bad args.

### `search::search_graphs`

**Purpose.** Find configured `Graph` rows. Embedded text is the
graph description plus its node ids - search for "review code"
might match a graph whose description mentions review and whose
nodes are `analyse`, `lint`, `verdict`.

### `search::search_collections`

**Purpose.** Find user-defined `Collection` rows. **Does not search
documents inside collections** - only the collection metadata rows.
To search documents within a specific user collection, use the
collection's own `system::search_collection`, or inspect documents
by path via `system::list_documents` + `system::get_document_content`.

### `search::search_tools`

**Purpose.** Find tools by name or description across every
reachable toolset (system, search, workspaces, misc, web, harness,
trigger, plus every user-defined toolset row). `document_id` is the
scoped id `<toolset_id>::<tool_id>`. Use this when you don't know
the exact tool name but know what you want to do.

### `search::search_ai_docs`

**Purpose.** Semantic search over agent-facing platform
documentation. Returns specific subsections (e.g. "Gotchas") of the
markdown docs shipped in `primer.ai_docs`. `document_id` is the slug
of the matched doc (`agents`, `triggers-and-subscriptions`, etc.);
`chunk_id` identifies which subsection within the doc matched.

**Returns.** Per hit: the chunk text (a specific section of the
doc), the score, and meta carrying the doc's title, summary, and
mcp_tools frontmatter - enough to decide which doc slug is the right
one. The returned chunk text is the readable payload; the AI-docs
bodies are not exposed through `system::get_document_content`, so a
follow-up search with a tighter query is how you pull in more of the
matched doc.

## Workflows

### Workflow 1 - find the right tool for a job

**Goal.** Agent needs to fetch a webpage; it knows roughly what it
wants but not the exact tool name.

1. Call `search::search_tools`:

```json
{
  "tool": "search::search_tools",
  "arguments": {"query": "fetch webpage HTTP get URL", "top_k": 5}
}
```

2. Hits include `web__http_request` near the top with a high score.
   Meta carries the toolset_id and the input_schema reference.
3. Now the agent has the scoped id and can call it directly:

```json
{
  "tool": "web__http_request",
  "arguments": {"method": "GET", "url": "https://example.com"}
}
```

### Workflow 2 - learn how a feature works before using it

**Goal.** Agent wants to set up a recurring trigger but isn't sure
about cron semantics in primer.

1. Search the docs first:

```json
{
  "tool": "search::search_ai_docs",
  "arguments": {"query": "cron trigger fire scheduled catch-up", "top_k": 3}
}
```

2. Top hit's `document_id` is `triggers-and-subscriptions`, chunk
   text covers the catch-up policy (`one` / `all` / `none`). Meta
   carries the doc's full mcp_tools list.
3. To pull in more of the matched doc, run a tighter follow-up
   search scoped to the area you still need:

```json
{
  "tool": "search::search_ai_docs",
  "arguments": {"query": "trigger catch-up policy one all none semantics", "top_k": 3}
}
```

4. Now the agent has the full mental model and can call
   `trigger::create` with the right config.

## Gotchas

- **The subsystem is opt-in.** A new install has no
  `InternalCollectionsConfig` row, no bootstrap, no search tools. If
  your `tools/list` doesn't contain `search::*`, that's why -
  operator needs to activate the subsystem.
- **`search::search_collections` searches collection *metadata*, not
  documents within collections.** To search documents in a specific
  user collection use that collection's own `system::search_collection`.
  This is a frequent mismatch.
- **First four collections store one vector per entity; the docs
  collection chunks.** A `search_agents` hit's `chunk_id` is always
  `"0"`; a `search_ai_docs` hit's `chunk_id` identifies a specific
  subsection. Code that assumes `chunk_id="0"` works for the first
  four and breaks on the fifth.
- **Asymmetric retrieval prefixes.** The embedder is called with
  `task_type="retrieval_query"` at search time and
  `task_type="retrieval_document"` at ingest. Models like BGE / E5
  add a query prefix internally that shifts the embedding sub-space.
  Mixing query and document tasks degrades recall - don't call
  `search::search_*` with a long copy-pasted document as the query.
- **Re-bootstrap is idempotent but not free.** Re-running bootstrap
  re-embeds every entity (skipping unchanged docs via content-hash).
  On large catalogues with paid embedding providers, this is real
  money. Operators get a confirmation prompt in the UI.
- **Embedding model change requires deactivate + re-bootstrap.**
  Each internal collection is stored in the vector store at a fixed
  dimension determined by the embedder that was active when bootstrap
  first ran. If you activate the subsystem with a different embedding
  model (or a different provider that happens to produce a different
  dimension), bootstrap fails with HTTP 422
  `type=/errors/dimension-mismatch` naming both dims and pointing to
  the deactivate endpoint. The fix: DELETE
  `/v1/internal_collections/config` (this drops the four internal
  collection tables from the backing SSP and clears the config row),
  then PUT a new config with the new model, then POST bootstrap.
- **Hits from `search_ai_docs` carry the chunk, not the whole doc.**
  Don't try to satisfy a user's question entirely from one snippet -
  the AI-docs bodies are not readable through
  `system::get_document_content`, so run a tighter follow-up
  `search_ai_docs` query to pull in the rest of the matched doc.
- **Tool documents use scoped ids as `document_id`.** The
  `_internal_tools` collection's document ids look like
  `system::create_agent`, not `create_agent`. This is so two
  different toolsets can both expose a `list` tool without colliding
  in the index.

## Related

- [knowledge](knowledge.md) - user-defined collections and documents,
  the parallel surface this doc explicitly is not about.
- [agents](agents.md) - `search_agents` searches these rows.
- [graphs](graphs.md) - `search_graphs` searches these rows.
- [mcp-exposure](mcp-exposure.md) - only tools in the operator's
  allowlist appear in `tools/list`; the searches above are the
  recommended starter set.
