---
slug: internal-collections
title: Internal collections
section: features
summary: The privileged collections subsystem that indexes primer's own entities for agent discovery.
---

## What sets them apart

A regular knowledge collection holds operator-supplied content.
An **internal collection** holds primer's own metadata: the
catalogue of agents, graphs, tools, knowledge collections, and
platform docs. The internal subsystem keeps these collections in
sync with the canonical entity store; the operator does not write
to them directly.

The point: agents can search 'which tool reads workspace files'
or 'is there an existing agent for this task' the same way they
would search any other collection.

```callout:warning
Internal collections are not a generic 'index everything'
mechanism. They are scoped to the entity types primer ships with.
Indexing your own content goes in a regular collection.
```

## How they get populated

Each entity-type has a publisher that watches the corresponding
storage table for CDC events and re-indexes the affected entity.
Inserts, updates, and deletes propagate to the internal
collection within seconds.

The publishers are wired at startup; they live in
`primer/internal_collections/`. If a subsystem is not enabled in
this primer instance (for example, the harnesses subsystem on a
trimmed deploy), the corresponding publisher does not run and
the internal collection stays empty.

## The search toolset

Agents reach internal collections through the `search` toolset:

| Tool | What it returns |
|---|---|
| `search_agents` | Agents matching the natural-language query |
| `search_graphs` | Graphs matching the query |
| `search_tools` | Tools across every bound toolset |
| `search_collections` | Knowledge collections by description |
| `search_ai_docs` | The agent-facing reference docs |

```code-tabs:python
--- python
# What an agent calls under the hood (not the operator API):
# search_agents(query="post-mortem summariser", k=3)
# returns: [{id: ..., name: ..., description: ..., score: ...}]
```

## Where to next

```ref:features/knowledge-collections
The regular collections feature page covers the operator-managed
side of the same machinery.
```
