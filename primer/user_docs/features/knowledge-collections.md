---
slug: knowledge-collections
title: Knowledge collections
section: features
summary: Create and manage knowledge collections in the console.
---

## Overview

A collection is a named container for documents that agents can
search via similarity. Two settings are fixed at create time and
cannot change afterwards: the embedding provider and model (which
decide how documents are vectorised) and the semantic search
provider (the database that holds the vector index).

Collections appear in the console under Knowledge / Collections.
The table shows each collection's ID, description, embedding
provider, and embedding model. Click any row to open the detail
panel on the right.

```embed:collection-list
```

## Creating a collection

1. Open Knowledge / Collections in the left navigation.
2. Click **New collection** (top-right of the filter bar).
3. Fill in the form that appears:
   - **ID** -- optional; if you leave it blank the backend assigns a type-prefixed id (e.g. `collection-3f9a1c8d`). Immutable after creation.
   - **Description** -- free text shown in the table.
   - **Embedding provider** -- pick from the providers configured
     under Providers / Embedding. The dropdown is empty if none
     are configured yet; create one there first.
   - **Model** -- options are drawn from the selected provider's
     declared model list.
   - **Search provider** -- the vector database (pgvector or
     pgvectorscale) that stores this collection's index.
     Immutable after create.
4. Click **Create**.

A success toast confirms the collection was created and the table
refreshes.

```callout:warning
The embedding model and search provider are bound at create time
and cannot be changed afterwards. If you need a different model,
delete the collection and create a new one; documents must be
re-ingested.
```

## Editing a collection

Select the collection row to open the detail panel, then click
**Edit**. Only the description can change on a non-system,
non-harness-managed collection. The ID, embedding model, and
search provider are locked.

System collections (marked with a system badge in the table) are
maintained automatically and cannot be edited or deleted by hand.

## Opening documents

From the detail panel, click **List documents** to open a modal
that pages through every document row (or vector entry for system
collections) stored in the collection. Use **Search** to run a
quick similarity query without leaving the panel.

## Filtering and refreshing

The filter bar at the top accepts a text string matched against
collection IDs. Click **Refresh** to re-fetch the list from the
API.

## Automate this

```ref:reference/api-knowledge
Full REST reference for collections: create, read, update,
list, and per-collection document and search endpoints.
```

## Related concept

```ref:concepts/toolsets-and-tools
How to bind a collection to an agent so the agent can call
search and retrieve against it.
```
