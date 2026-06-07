---
slug: semantic-search
title: Semantic search
section: features
summary: Run semantic search over a knowledge collection in the console.
---

## Overview

The console exposes a search modal on every collection that lets
you run natural-language queries against the collection's vector
index and inspect the ranked results. This is useful for verifying
that documents are indexed correctly, tuning top-k, and spot-checking
retrieval quality before binding the collection to an agent.

Search is available from the collection detail panel on the
Knowledge / Collections page. It runs against the same endpoint
agents use, so results here reflect what an agent would see.

## Running a search

1. Open Knowledge / Collections in the left navigation.
2. Click the collection row you want to search to open the detail
   panel on the right.
3. Click **Search** in the detail panel action buttons.
4. The search modal opens with a query textarea and a `top_k` field
   (default 10).
5. Type a natural-language query in the textarea.
6. Press **Enter** (or Shift+Enter for a newline) or click the
   **Search** button.
7. Results appear below the search bar, one entry per chunk.

```callout:tip
The modal shows response latency in milliseconds and the hit count
next to the top_k control after each query. Use this to compare
retrieval speed across collections backed by different search
providers.
```

## Reading the results

Each result row shows:

- **Rank** -- position in the result set (1-based).
- **Document ID** -- the document the chunk belongs to.
- **Chunk ID** -- the sub-document chunk identifier.
- **Score** -- cosine similarity score, four decimal places.
- **Text** -- the chunk text as indexed.
- **Metadata** -- any metadata keys stored on the chunk, rendered
  inline below the text.

Low scores (near 0) mean weak semantic overlap with the query.
If top results are irrelevant, check the embedding model bound
to the collection and whether the source documents were converted
cleanly before ingest.

## Adjusting top_k

The `top_k` field controls how many chunks the vector index
returns. Increase it to see more candidates when diagnosing
recall issues; decrease it to tighten results for a focused
query. The field accepts values from 1 to 100.

## Reranking

If the collection's search provider has a cross-encoder reranker
configured (set up on the Internal Collections page), results
are reranked automatically before being returned. The console
search modal reflects post-rerank order; the score column still
shows the original vector similarity score, not the reranker
score.

```callout:warning
The search modal queries the live index. Results change as
documents are ingested or deleted. Run the search again after
adding content to confirm new material appears.
```

## Automate this

```ref:reference/api-knowledge
Full REST reference for the collection search endpoint, including
query payload schema and response shape.
```
