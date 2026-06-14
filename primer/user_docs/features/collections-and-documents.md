---
slug: collections-and-documents
title: Collections and documents
section: features
summary: Create knowledge collections with a fixed embedder and SSP, ingest plaintext or uploaded files, and search with optional MMR and cross-encoder reranking.
---

## Concept

A **collection** is a named container of documents that you can search by meaning. Every collection has an embedding model and a semantic search provider (SSP) chosen at creation time. When you add a document to a collection, Primer splits the document text into overlapping chunks, passes each chunk through the embedding model to produce a dense vector, and stores those vectors in the collection's SSP. At search time, the query is embedded with the same model and the SSP returns the chunks whose vectors are nearest in meaning.

A **document** is a piece of text you add to a collection. It has an ID, a name, free-form metadata (a JSON object), and a text payload. The payload is split into chunks on ingest; the chunks, not the document as a whole, are what the vector index holds and returns as search results.

Two decisions are fixed at create time and cannot be changed afterward:

- **Embedder** (provider + model): all chunks in a collection must live in the same vector space. Changing the embedder would invalidate the existing index.
- **Semantic search provider**: chunks are written to the chosen backend. Migrating them to a different backend would require reindexing all documents.

Other collection settings (description, MMR diversification, and cross-encoder reranking) can be updated at any time.

## Configuration

### Collection fields

| Field | Editable after create | Notes |
|---|---|---|
| ID | no | Alphanumeric plus hyphens and underscores. Auto-generated if omitted (format: `collection-<hex12>`). |
| Name / description | yes | Human-readable label for the console. |
| Embedding provider | no | References an EmbeddingProvider row by ID. |
| Embedding model | no | One of the models permitted on the referenced provider. |
| Semantic search provider | no | References an SSP row by ID. |
| MMR diversification | yes | Maximal Marginal Relevance tuning (see below). |
| Cross-encoder reranker | yes | Cross-encoder reranking config (see below). |

### MMR diversification

MMR (Maximal Marginal Relevance) diversifies search results so near-duplicate chunks do not all surface together. It is cheap (pure linear algebra over the candidate pool) and adds essentially no latency for pools under a few hundred chunks.

| Field | Default | Notes |
|---|---|---|
| lambda_mult | 0.5 | Diversity/relevance trade-off. `1.0` = pure relevance (equivalent to no MMR). `0.0` = pure diversity. `0.5` is the conventional balanced default. |
| fetch_k | auto | Candidates pulled from the vector store before MMR runs. When blank, the searcher uses `max(50, 10 * k)`. Set explicitly to control the overfetch size. |

### Cross-encoder reranking

A cross-encoder reranker reads the query and each candidate chunk together and produces a fine-grained relevance score. It gives higher accuracy than pure vector similarity at the cost of extra compute per query.

| Field | Default | Notes |
|---|---|---|
| Provider | required | References a CrossEncoderProvider row by ID. |
| Model | required | One of the models listed on the referenced provider. |
| top_n | 100 | How many vector-search candidates the cross-encoder scores. Quality plateaus past ~100 in benchmarks; latency grows linearly beyond that. |
| batch_size | 32 | Batch size handed to the cross-encoder model. 32 is the sentence-transformers default for CPU; 64--128 is typical on GPU. |

### Search pipeline order

When both MMR and a cross-encoder are configured, the pipeline runs in this fixed order:

```mermaid
flowchart LR
    query["Query"] --> embed["Embed query"]
    embed --> store["Vector store\n(top N candidates)"]
    store --> cer["Cross-encoder\n(score + re-sort, top_n)"]
    cer --> mmr["MMR\n(diversify, top k)"]
    mmr --> results["Results"]
```

The cross-encoder needs a relevance-rich pool, so it runs before MMR. MMR then diversifies the already-relevant pool. Running them in the opposite order would waste cross-encoder compute on diverse-but-irrelevant items.

## Walkthrough

### Create a collection

1. In the console, open **Knowledge** and click **Collections**.
2. Click **New collection**.
3. Enter an ID (or leave blank to auto-generate) and a description.
4. Choose an **Embedding provider** and **model**. Both are fixed after you save.
5. Choose a **Semantic search provider**. This is also fixed after you save. If you have not registered an SSP, the reserved `lance` row is always available.
6. Optionally enable **MMR** and/or a **Cross-encoder reranker** by toggling their sections and filling in the fields.
7. Click **Save**.

```embed:collection-create
```

The collection row is created. The vector index for this collection does not exist yet in the backend; the SSP creates it lazily when the first document is ingested.

### Add a document

1. Open the collection and click **Documents**.
2. Click **Add document**.
3. Enter a document ID (or leave blank to auto-generate) and a name.
4. Paste the document text directly, or upload a file. Supported upload formats include PDF, DOCX, HTML, and Markdown; Primer converts them to text before splitting.
5. Optionally add metadata as a JSON object (e.g. `{"source": "manual", "version": "2"}`). Metadata is stored with every chunk and can be used for filtering.
6. Click **Save**.

Primer splits the document into overlapping chunks, embeds each chunk, and writes the vectors to the SSP. Progress is synchronous; the save button returns once indexing is complete.

**Tip:** You can also drop multiple files at once in the upload area to queue them as a batch.

### Edit a collection

1. Open the collection and click **Edit**.
2. Update the description, MMR settings, or cross-encoder settings.
3. Click **Save**.

The embedding provider, model, and SSP fields are read-only in the edit form. To change them, delete the collection and create a new one with the desired settings, then re-ingest the documents.

### Edit a document

1. Open the document and click **Edit**.
2. Update the name, metadata, or text payload.
3. Click **Save**.

Saving with a changed text payload triggers a full re-index: Primer deletes the document's existing chunks from the vector store and re-ingests the new text. The collection ID and document ID are locked after creation.

### Delete a document

Deleting a document removes its row and its chunks from the vector store. The chunks are dropped immediately; there is no soft-delete.

### Search a collection

1. Open the collection and click the **Search** button.
2. Enter a query and optionally adjust **k** (the number of results, 1-100).
3. Click **Search**.

Results show the document ID, chunk ID, score, text, and metadata for each hit. If a cross-encoder is configured on the collection, the score column reflects the cross-encoder logit (higher = more relevant); otherwise it reflects the raw vector similarity from the SSP.

## Using collections from an agent

The console search above is for operators. An agent reaches collections and documents through tools instead. Add the ones it needs on the agent's Tools tab, then it can:

- **Find the right collection**: `search__search_collections` runs a semantic search over your collection *definitions* and returns the collections whose description best matches a query. It is part of the `search` toolset, which is only available when internal collections are enabled.
- **Find documents in a collection**: `system__list_collection_documents` lists a collection's documents, and `system__find_collection_documents_by_meta` filters them by metadata fields.
- **Read a document**: `system__get_document_content` returns a document's full text by `document_id`.

A typical flow is: call `search__search_collections` to locate a knowledge base, list or metadata-filter its documents, then call `get_document_content` on the ones the task needs.

```callout:note
Agent-driven semantic search over a collection's document *contents* (`system__search_collection`) is not yet wired to the search pipeline and currently returns a not-implemented error; use `find_collection_documents_by_meta` for now. The operator search bench above is unaffected.
```

```ref:features/embedding-providers
```

```ref:features/semantic-search-providers
```

```ref:features/cross-encoder-providers
```

```ref:features/internal-collections
```
