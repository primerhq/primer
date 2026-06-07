---
slug: internal-collections
title: Internal collections
summary: Configure the internal-collections subsystem to enable semantic search across agents, graphs, tools, and knowledge collections.
section: features
---

## Overview

The internal-collections subsystem maintains four reserved vector collections that mirror primer's own entity catalogue: agents, graphs, tools, and knowledge collections. Once active, agents can issue natural-language searches against these collections using the `search` toolset (`search_agents`, `search_graphs`, `search_tools`, `search_collections`, `search_ai_docs`). The four `/v1/{kind}/search` routes return `503` until the subsystem is active.

The subsystem has three states: **inactive** (no config row), **configured** (config saved but bootstrap not yet run), and **active** (bootstrap completed, search routes live).

## Configure and activate

1. Navigate to **Internal Collections** in the sidebar.
2. If the subsystem is inactive the page shows "Internal Collections is not configured." Click **Configure**.
3. In the Configure modal, fill in the required fields:
   - **Semantic Search provider** -- pick the SSP that will back the four reserved collections. You must create an SSP first if the list is empty.
   - **Embedding provider** -- pick the provider for generating embeddings.
   - **Embedding model** -- select the model from the provider's list.
4. Optionally enable **MMR diversification** and set the lambda (0-1). Optionally enable a **Cross-encoder reranker** and pick its provider and model.
5. Click **Save**. The page transitions to the configured state with a warning that bootstrap is required.
6. Click **Bootstrap now**. A progress panel appears showing the current phase (draining CDC queue, materialising collections, ingesting agents/graphs/collections/tools, finalising) with a progress bar and per-entity counts.
7. Bootstrap runs in a background task on the server. You can navigate away; the progress panel resumes when you return. When bootstrap completes the page transitions to the active state (green header).

```callout:warning
The SSP, embedding provider, and embedding model are locked once the subsystem is activated. Changing them requires deactivating first: the config row is removed and all four reserved collections are dropped. Re-configure and re-bootstrap to rebuild from scratch. Cross-encoder and MMR settings remain editable at any time.
```

## Update config and re-bootstrap

While active, click **Update config** to change cross-encoder or MMR settings. To force a full re-index (for example after bulk entity changes), click **Re-bootstrap**. The subsystem stays live during re-bootstrap; search results may be stale until it completes.

## Deactivate

Click **Deactivate** to remove the config row and drop all four reserved collections. The CDC worker stops; new entities will not be indexed. All four search routes immediately return `503`. This is the required path before switching embedding providers or models.

## Automate this

```ref reference/api-knowledge
The API reference covers the PUT /internal_collections/config, POST /internal_collections/bootstrap, and DELETE /internal_collections/config endpoints with full schema detail.
```

```ref features/knowledge-collections
The regular knowledge collections feature page covers operator-managed collections backed by the same SSP machinery.
```
