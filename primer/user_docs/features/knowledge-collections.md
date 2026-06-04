---
slug: knowledge-collections
title: Knowledge collections
section: features
summary: Collections as the unit of RAG; create, populate, search, share.
---

## What a collection is

A collection is a named container for documents that agents can
search. Two settings are durable per collection: the semantic
search provider it binds to (which embedding model indexes its
documents) and the access policy (who can read, write, search).

A collection is also the granularity of agent binding. An agent
sees only the collections the operator has bound to it; an
operator with two unrelated projects keeps the collections
separate so the agents do not leak context.

## The empty state

The Collections page on a fresh install looks like this:

```mockup:collection-list-empty
{ "emptyLine": "No collections yet" }
```

## Creating a collection

The console New collection button opens a small form: name,
description, semantic-search provider, retention. The same
operation via the API:

```code-tabs:python,curl,javascript
--- python
col = client.knowledge.create_collection(
    name="incident-runbooks",
    description="Post-mortem write-ups and runbook entries.",
    ssp_id="voyage-3-large",
)
print(col.id)
--- curl
curl -X POST https://primer.example/v1/knowledge/collections \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "name":"incident-runbooks",
    "description":"Post-mortem write-ups...",
    "ssp_id":"voyage-3-large"
  }'
--- javascript
const r = await fetch("/v1/knowledge/collections", {
  method: "POST",
  headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
  body: JSON.stringify({
    name: "incident-runbooks",
    description: "Post-mortem write-ups...",
    ssp_id: "voyage-3-large",
  }),
});
```

## Binding to an agent

Once a collection exists, bind it to one or more agents from the
Agents page (Tools tab). The agent can then call `search_collection`
and `get_document` for any document in any bound collection.

```ref:concepts/toolsets-and-tools
The binding model that decides which tools each agent can reach.
```

## Document ingest

Adding a document to a collection runs it through the ingestion
pipeline: chunk, embed, index. The chunking strategy is per
collection; the default is a paragraph-aware splitter targeting
~800 tokens per chunk.

```callout:tip
Tune the chunk size to the use case before populating. Reflowing
1000 documents through a new chunker is expensive; smaller
chunks for FAQ-style content, larger chunks for prose-heavy
collections.
```

## Where to next

For the document-level walkthrough (upload, chunk strategy,
re-index):

The document feature page covers ingest mechanics, the per-doc
metadata schema, and the retrieval surface in detail. Phase E
of the doc rollout ships it alongside this page.
