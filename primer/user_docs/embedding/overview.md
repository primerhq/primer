---
slug: embedding-overview
title: Embedding & Semantic Search
section: embedding
summary: The pipeline that turns documents into searchable vectors: embedding providers, vector stores, collections, cross-encoder reranking, and internal collections.
---

## The pipeline

Semantic search in primer is a small pipeline of independently-configured pieces:

1. An **embedding provider** turns text into vectors.
2. A **semantic search provider** is the vector store that holds those vectors and returns the nearest neighbours for a query.
3. A **collection** ties an embedder and a vector store together and holds the documents you ingest. Documents are chunked, embedded, and stored on ingest.
4. At query time the embedder vectorises the query, the store returns top-k nearest chunks, and an optional **cross-encoder** reranks those candidates for precision.

**Internal collections** apply the same machinery to primer's own catalogue (agents, providers, and the like) so agents can search the platform itself.

```ref:embedding/embedding-providers
Register embedding providers that turn text into vectors.
```

```ref:embedding/semantic-search-providers
Register the vector store that holds embeddings and serves nearest-neighbour queries.
```

```ref:embedding/collections-and-documents
Create collections, ingest documents, and run searches over them.
```

```ref:embedding/cross-encoder-providers
Add a cross-encoder reranker to refine the top-k results.
```

```ref:embedding/internal-collections
Make primer's own catalogue searchable as internal collections.
```
