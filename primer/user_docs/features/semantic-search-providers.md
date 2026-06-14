---
slug: semantic-search-providers
title: Semantic search providers
section: features
summary: Register semantic search providers (pgvector, pgvectorscale, or LanceDB) so collections have a vector store to write and search against.
---

## Concept

Every knowledge collection stores its document chunks as dense vectors and retrieves them by similarity. A **semantic search provider (SSP)** is the backend that holds those vectors and answers nearest-neighbour queries. Primer separates the SSP from the collection so you can run several backends simultaneously -- a lightweight local store for development and a Postgres-backed store for production -- and bind each collection to whichever backend fits its workload.

Three backends ship with Primer:

- **LanceDB** (`lance`) -- an embedded, file-backed vector store with no external dependency. Primer auto-creates a reserved `lance` row at first boot so semantic search works without any manual configuration. Use it for development, local demos, or workloads where a flat-file backup (copy the directory) is sufficient.
- **pgvector** (`pgvector`) -- vectors stored in Postgres tables alongside the rest of your data. A single Postgres instance serves both storage and semantic search; suitable for small to medium collections.
- **pgvectorscale** (`pgvectorscale`) -- pgvector extended with the StreamingDiskANN index and Statistical Binary Quantization. Choose this backend for very large collections (10 million+ vectors) where HNSW memory cost becomes prohibitive.

Once you register an SSP, you bind a collection to it at create time. That binding is immutable: every embedded chunk for that collection lives in that backend. Deleting an SSP that still has collections bound to it returns 409.

```mermaid
erDiagram
    SemanticSearchProvider ||--o{ Collection : "backs (search_provider_id)"
    SemanticSearchProvider ||--|| VectorStore : "constructs"
    Collection }o--|| EmbeddingProvider : "uses embedder"
    Collection ||--o{ EmbeddingRecord : "indexed chunks"
    VectorStore ||--o{ EmbeddingRecord : "stores + ranks"
```

The search pipeline works as follows: an agent or the console search modal embeds the query text using the collection's configured embedder, sends the resulting vector to the SSP's vector store, and the store returns the top-k nearest chunks by similarity. That ranked list can be further refined by a cross-encoder reranker or MMR diversification -- both are configured on the collection, not the SSP (see ref:features/collections-and-documents).

## Configuration

### LanceDB fields

| Field | Default | Notes |
|---|---|---|
| Path | required | Absolute filesystem path for the LanceDB datasets. Created with mode 0700 on first use. The directory is your backup unit -- copy it to snapshot the index. |
| HNSW M | 16 | Graph degree. Higher values improve recall and increase index size. |
| HNSW ef construction | 64 | Candidate list size during index build. Higher = better recall, slower build. |
| HNSW ef search | 40 | Candidate list size during queries. Higher = better recall, slower queries. |
| Index min rows | 1000 | Brute-force search is used below this threshold; the ANN index is built lazily once the collection crosses it. |

The reserved `lance` row uses `~/.primer/vector` as its path. You can add additional LanceDB SSPs at different paths to partition workloads into separate directories.

### pgvector fields

| Field | Default | Notes |
|---|---|---|
| DSN | required | Postgres connection string (e.g. `postgresql://user:pass@host/db`). The `vector` extension is installed automatically on first use. |
| HNSW M | 16 | See LanceDB above. |
| HNSW ef construction | 64 | See LanceDB above. |
| HNSW ef search | 40 | See LanceDB above. |
| Reindex cron | none | Crontab expression for periodic HNSW maintenance. Leave blank to disable. |
| Use halfvec | off | Store vectors as pgvector `halfvec` (half-precision, up to 4000 dimensions) instead of the standard `vector` type (up to 2000 dimensions). Enable for embedding models above 2000 dimensions -- for example `text-embedding-3-large` outputs 3072-dimensional vectors. **Only affects collections created while this flag is on**; existing collections keep their original type. |

### pgvectorscale fields

pgvectorscale shares all pgvector fields and adds the following DiskANN controls (active only when **Enable DiskANN** is on):

| Field | Default | Notes |
|---|---|---|
| Enable DiskANN | off | When on, collections get a StreamingDiskANN index instead of HNSW. Recommended for 10 million+ vectors. |
| Storage layout | `memory_optimized` | `memory_optimized` enables Statistical Binary Quantization (SBQ) to reduce memory; `plain` keeps full-precision vectors in the index. |
| Num neighbors | 50 | DiskANN graph degree. Higher = better recall, larger index. |
| Search list size | 100 | Build and query-time candidate list. Higher = better recall, slower build and queries. |
| Max alpha | 1.2 | Graph density. Values up to ~1.4 increase recall at the cost of build time. |
| Num bits per dimension | auto | Bits per dimension used by SBQ. Leave blank to use the pgvectorscale default. Ignored when storage layout is `plain`. |

## Walkthrough

### Add a pgvector SSP

1. In the console, open **Providers** in the left navigation and click the **Semantic Search** tab.
2. Click **Add provider**.
3. Choose **pgvector** from the provider type dropdown.
4. Enter an ID (e.g. `pgvector-prod`). IDs are immutable after creation.
5. Enter the Postgres DSN for your database.
6. Adjust HNSW tuning if needed -- defaults work for most workloads.
7. Enable **Use halfvec** if you plan to use embedding models with more than 2000 dimensions.
8. Click **Save**.

```embed:ssp
```

The provider row is saved and the backend runs `CREATE EXTENSION IF NOT EXISTS vector` on first use. The SSP appears in the provider list and is immediately selectable when creating a collection.

### Add a LanceDB SSP

Follow the same steps, choose **lance** as the provider type, and supply an absolute path on a filesystem where Primer has write access. The directory is created on first use.

### Invalidate a provider

If you rotate database credentials or want to force the server to re-open a connection, click **Invalidate** next to the provider row (or send `POST /v1/ssp/{id}/invalidate`). The cached adapter is closed and rebuilt on the next request. No vector data is lost.

## What happens after

Once an SSP is registered, it appears in the **Semantic search provider** dropdown when you create a collection. That binding is permanent: all document chunks for that collection are written to, and searched from, that backend for the lifetime of the collection.

The reserved `lance` row is always available out of the box. Its row cannot be deleted (returns 403) and its id cannot be reused by a new provider row (returns 409). All other SSPs can be deleted once no collection references them.

```ref:features/embedding-providers
```

```ref:features/collections-and-documents
```

```ref:features/cross-encoder-providers
```

```ref:features/internal-collections
```
