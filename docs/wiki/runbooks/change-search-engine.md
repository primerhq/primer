---
title: Change the search engine
summary: Point the platform's semantic search at a different vector store; register the provider, re-point the collection, verify a search hits.
audience: admin
tools: [system__create_semantic_search_provider, system__list_semantic_search_providers, system__invalidate_semantic_search_provider]
related: [purpose-map, knowledge, runbooks/conventions, not-agent-callable]
---

## Goal
"Change the search engine": the semantic search the collections use runs on a semantic search provider (the vector store). This runbook points it at a different one.

## Preconditions
- the target backend is available to this install: `pgvector` and `pgvectorscale` need the Postgres the install runs on (with the extension), `lance` is a local backend.
  - check: the install's live map (the live map under [this-install](this-install)) names the database and the providers of this install; the concept page under [knowledge](knowledge) states which backends this install's database supports.
- where the collection being re-pointed needs an embedding provider: one exists and answers.
  - check: the embedding provider row exists and its test passes (the embedding how-to is the sibling pending page, M2): a collection cannot search semantically without embeddings.

## Roles and gates
- runner: `admin`.
- gated steps (a fresh install ships; the live gate is read with `system__describe_tool` at run time):
  - no step in this flow ships a gate (the writes are admin-role configuration); a running install may have added gates.

## Steps
### 1. Read the current provider
Call `system__list_semantic_search_providers` ([reference](reference/tools/system/list_semantic_search_providers)) -> expected: the provider rows, each with its id, backend type and config. Note which one the collections in scope use.

### 2. Register the new provider
Call `system__create_semantic_search_provider` ([reference](reference/tools/system/create_semantic_search_provider)) with the target backend:

```json
{"toolset_id": "system", "tool_name": "create_semantic_search_provider", "arguments": {"id": "<new provider id>", "provider": "<pgvector|pgvectorscale|lance>", "config": {"<the backend's connection fields>"}}}
```

Expected: the row. (The per-backend config shape is documented on the generated reference page for the tool and the [knowledge](knowledge) concept page.)

### 3. Point the collection's search at it
The collection's search binding is a REST write of the collection (the collection's search config: which embedding provider and which semantic search provider back its search): `PUT /v1/collections/{id}/search`. No agent tool reaches this surface: it is the human step of the flow, named under [not-agent-callable](not-agent-callable) - the operator states the exact body (the provider ids and model names) and the admin applies it, or the flow stops here and says so.

### 4. Watch the indexing state and verify a search
After the re-point, the collection re-indexes its documents into the new store: watch the collection's indexing state until it settles, then run a search that must hit:

- the behavioral check: a search on a known phrase from a known document of the collection returns that document.

## Verification
- `system__list_semantic_search_providers` lists the new provider; the collection's search config (the REST read of the collection) names it.
- the indexing state has settled (no in-flight re-index).
- the behavioral search returns the known document (hits > 0 for a phrase only the document contains).

## Refusal and failure
- type=validation-error (the provider config) -> the backend's connection fields are wrong for this install (a pgvector backend against a database without the extension): read the [knowledge](knowledge) page for the backend's requirements and pick the backend the install supports.
- the re-point stalls in indexing -> the store is unreachable: the provider row's config is the thing to check; `system__invalidate_semantic_search_provider` ([reference](reference/tools/system/invalidate_semantic_search_provider)) drops a provider's cached state when it answers stale.
- a search returns no hits after the index settled -> the embeddings are missing or mismatched (the collection's embedding provider does not answer): the precondition check is the read that proves it.

## Related
- [purpose-map](purpose-map): the index row this runbook serves.
- [knowledge](knowledge): the collection search concept.
- [runbooks/configure-provider](runbooks/configure-provider): the sibling configuration (the LLM provider and model profile).
