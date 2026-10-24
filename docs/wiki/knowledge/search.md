---
title: "Search a collection"
summary: "Search a collection by meaning, keywords, exact text or pattern, and what each mode needs."
order: 3
tools: [collections__search, system__search_collection]
related: [knowledge/navigation, providers/semantic-search, knowledge/the-platform-wiki]
---
## What it is
Search finds the documents in a collection: by meaning when the collection's semantic index is ready, by keywords when it is not, and by exact text or pattern whenever. One `collections` tool covers every rung and states which one it used; the older `system` tool runs the semantic rung alone.

## Concepts
- The modes of `collections__search`: `auto`, `semantic`, `fulltext`, `literal` and `regex`. The answer always states `mode_used`, so you know which rung answered.
- `auto` picks the best rung the collection offers: semantic when its `search` block is set and its `state` is `ready`, otherwise the fulltext rung. When search is on but not ready, the answer carries a note naming the state it fell back from.
- `semantic` needs a ready `search` block. An explicit `semantic` on a collection without one fails with a conflict naming the modes that are available: `fulltext`, `literal`, `regex`.
- `fulltext` is keyword-relevant and works on every collection, with or without search on. `literal` is an exact substring test, with no length cap. `regex` is a pattern, at most 1000 characters, with a 10 second matching budget: a pattern that backtracks past it is refused.
- `path_prefix` restricts a search to paths under a prefix, and `limit` caps the hits (1 to 100, default 20).
- Hits are chunks, not whole documents: a fulltext hit is keyword-relevant, a semantic hit is meaning-relevant. Read the whole document at [navigate and read documents](knowledge/navigation).
- `system__search_collection` is the older, semantic-only call. It vectorises the query with the collection's own embedder and searches the collection's vector store, the same path the console route uses, and applies the collection's rerank setting when the `search` block sets a cross-encoder. It answers ranked chunk hits keyed by `document_id` and `chunk_id`, an empty list when nothing is indexed yet, and a conflict when the collection has no search block.
- The rerank and chunking settings live in the collection's `search` block: an admin changes them with the collection, not with a search call. The providers behind it are registered at [semantic-search providers](providers/semantic-search).

## How to
1. See which rung a collection offers: `collections__collections_list` names `semantic` or `fulltext` per collection.
2. Search with `collections__search` and `mode` left at `auto`.
3. Read `mode_used` in the answer. A `fulltext` answer with a note means the semantic index is still `indexing` or in `error`: wait, or read the `error` hint on the collection.
4. For an exact string use `mode: "literal"`. For a pattern use `mode: "regex"`, and narrow `path_prefix` when a pattern is slow.

## Tool calls
### Search a collection
`collections__search` runs one search over every rung; `mode` defaults to `auto` and the answer states `mode_used`.

```json
{"toolset_id": "collections", "tool_name": "search", "arguments": {"collection": "kb", "query": "how refunds work"}}
```

Reference: [`collections__search`](reference/tools/collections/search)

### Semantic search, the older call
`system__search_collection` runs the semantic rung alone over a collection's indexed contents.

```json
{"toolset_id": "system", "tool_name": "search_collection", "arguments": {"collection_id": "kb-1", "query": "onboarding"}}
```

Reference: [`system__search_collection`](reference/tools/system/search_collection)

## Gotchas
- A collection still `indexing` returns partial results: `auto` falls back to fulltext with a note, and an explicit `semantic` is refused until the state is `ready`.
- Use `literal` for an exact string and `regex` only when you mean a pattern; a backtracking-heavy pattern is refused, so simplify it or narrow `path_prefix`.
- `system__search_collection` is semantic-only: on a grep-only collection it answers a conflict, and `collections__search` with `fulltext`, `literal` or `regex` is the way in.
- A hit names a chunk: the `path` in a `collections__search` hit and the `document_id` in a `system__search_collection` hit point at the document you read to see the rest.

## Related
[Navigate and read documents](knowledge/navigation), [Semantic-search providers](providers/semantic-search), [The platform wiki](knowledge/the-platform-wiki).
