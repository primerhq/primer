---
slug: internal-collections
title: Internal collections
section: features
summary: Configure the internal-collections subsystem to enable semantic search across agents, graphs, tools, and knowledge collections.
---

## What internal collections are

Primer indexes its own entity catalogue into four reserved vector collections:
agents, graphs, tools, and knowledge collections. Once this subsystem is active,
agents can issue natural-language searches against these collections using the
`search` toolset tools (`search_agents`, `search_graphs`, `search_tools`,
`search_collections`, `search_ai_docs`). The four `/v1/{kind}/search` routes
return `503` until the subsystem is active.

This is the mechanism behind the dogfooding pattern from the quickstart: an
agent that can search for the right agent or tool by description rather than
having the entire catalogue stuffed into its system prompt. The catalogue stays
out of context; the agent retrieves only the small slice it needs at query time.

The subsystem uses the same embedding and search provider machinery as
operator-managed knowledge collections, but the four reserved collections are
owned and maintained by the platform itself. A CDC (change-data-capture) worker
keeps them up to date as entities are created, updated, or deleted.

### Three states

| State | What it means |
|---|---|
| **Inactive** | No config row exists. Search routes return `503`. |
| **Configured** | Config saved but bootstrap not yet run. Search routes still return `503`. |
| **Active** | Bootstrap completed. Search routes return results. CDC worker is running. |

### Dimension mismatch on activation

The SSP, embedding provider, and embedding model are locked once the subsystem
is bootstrapped. If you try to activate with a model whose embedding dimension
differs from the dimension already stored in the collections, the bootstrap
returns a `422` with a `DimensionMismatchError`. The error message names the
stored dimension and the new model's dimension. To resolve it, deactivate first
(drops the four reserved collections), then re-configure with the intended
model and re-bootstrap.

## Configuration

Three fields are required to configure the subsystem:

| Field | Description |
|---|---|
| **Semantic search provider** | The SSP that will back the four reserved collections. Create an SSP first if the list is empty. |
| **Embedding provider** | The provider used to generate embeddings for ingestion and queries. |
| **Embedding model** | The model from the selected provider's list. |

Two optional enhancements:

| Field | Description |
|---|---|
| **MMR diversification** | Maximum marginal relevance reranking. Enable and set a lambda (0-1, where 1 = pure relevance, 0 = pure diversity). |
| **Cross-encoder reranker** | A second-pass reranker applied after vector search. Pick the provider and model. |

Once the subsystem is active, the SSP, embedding provider, and embedding model
are locked. Cross-encoder and MMR settings remain editable at any time via
**Update config** without requiring a re-bootstrap.

## Walkthrough: activate internal collections

1. Navigate to **Internal Collections** in the sidebar.
2. If the subsystem is inactive, the page shows "Internal Collections is not
   configured." Click **Configure**.

```embed:internal-collections-enable
```

3. In the Configure modal, fill in the required fields:
   - **Semantic Search provider**: pick an existing SSP, or follow the link
     to create one. The local `lance` provider works without any API key.
   - **Embedding provider**: pick an existing embedding provider.
   - **Embedding model**: select the model from the provider's list.
4. Optionally enable **MMR diversification** and set the lambda. Optionally
   enable a **Cross-encoder reranker** and pick its provider and model.
5. Click **Save**. The page transitions to the configured state with a warning
   that bootstrap is required.
6. Click **Bootstrap now**. A progress panel appears showing the current phase:
   draining CDC queue, materialising collections, ingesting agents, graphs,
   collections, and tools, then finalising.

Bootstrap runs as a background task on the server. You can navigate away; the
progress panel resumes when you return. When bootstrap completes, the page
transitions to the active state (green header) and the search routes go live.

```callout:warning
The SSP, embedding provider, and embedding model are locked once the subsystem
is activated. Changing them requires deactivating first: the config row is
removed and all four reserved collections are dropped. Re-configure and
re-bootstrap to rebuild from scratch. Cross-encoder and MMR settings remain
editable at any time.
```


```ref:features/semantic-search-providers
Create and configure the semantic search provider that backs the four reserved
collections.
```

```ref:features/collections-and-documents
Operator-managed knowledge collections use the same SSP and embedding
machinery.
```

```ref:reference/api-knowledge
REST API for PUT /internal_collections/config, POST /internal_collections/bootstrap,
and DELETE /internal_collections/config with full schema detail.
```
