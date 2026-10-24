---
title: "Cross-encoder providers"
summary: "Admin: register the rerankers that reorder search results."
audience: admin
order: 4
tools: [system__list_cross_encoder_providers, system__get_cross_encoder_provider, system__create_cross_encoder_provider, system__update_cross_encoder_provider, system__delete_cross_encoder_provider, system__find_cross_encoder_providers, system__fetch_cross_encoder_provider_models, system__test_cross_encoder_provider]
related: [providers/embedding, knowledge/search]
---
## What it is
A `CrossEncoderProvider` row is a reranker: it re-scores and re-orders the candidates a semantic search returns. The only backend is `huggingface`. Every tool on this page needs an admin.

## Concepts
- Hugging Face runs local sentence-transformers rerankers. `config` is an optional `token`, needed only for gated repositories; public rerankers such as `BAAI/bge-reranker-v2-m3` need none.
- The row lists its `models`, at least one, each a `name` plus an optional `max_pair_length`: a cap on the (query + document) token length the model consumes per pair. Absent, the model's own default applies.
- A collection's search block names the provider, one of its models and `top_n`, the number of candidates it scores (default 100); `batch_size` defaults to 32. Left unset, a collection runs raw vector search. The block is validated against your configured providers at runtime.
- The id `huggingface-ce` is reserved: bootstrap seeds the local reranker under it, a create with that id is a 409, and its delete is a 403.
- `limits` is the shape of the other provider families: a required `max_concurrency` plus the timeout and retry defaults.
- `system__test_cross_encoder_provider` checks a draft's models on the Hub before you register them and stores nothing: the answer is a per-model `ok` and `status`, and a draft that cannot be a row is an answer (`{ok: false, error: ...}`), not a tool error. `provider` may be left out: `huggingface` is the only kind.
- `system__fetch_cross_encoder_provider_models` lists what the server offers; it differs from the row's permitted models.

## How to
1. Test the draft when you are not sure the models exist, or that the token can pull them.
2. Create the provider with its models and limits.
3. Name the provider and a model in a collection's search block; its candidates are then re-scored before results are returned.

## Tool calls
### Test a draft provider
`system__test_cross_encoder_provider` checks the draft's models on the Hub; it stores nothing.

```json
{"toolset_id": "system", "tool_name": "test_cross_encoder_provider", "arguments": {"config": {}, "models": [{"name": "BAAI/bge-reranker-v2-m3", "max_pair_length": 512}]}}
```

Reference: [`system__test_cross_encoder_provider`](reference/tools/system/test_cross_encoder_provider)

### Create a provider
`system__create_cross_encoder_provider` registers the reranker with its models. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_cross_encoder_provider", "arguments": {"entity": {"id": "ce-1", "provider": "huggingface", "models": [{"name": "BAAI/bge-reranker-v2-m3"}], "config": {"token": "hf-REPLACE"}, "limits": {"max_concurrency": 4}}}}
```

Reference: [`system__create_cross_encoder_provider`](reference/tools/system/create_cross_encoder_provider)

### Fetch the models it offers
`system__fetch_cross_encoder_provider_models` lists what the server serves now; it differs from the row's permitted models.

```json
{"toolset_id": "system", "tool_name": "fetch_cross_encoder_provider_models", "arguments": {"provider_id": "ce-1"}}
```

Reference: [`system__fetch_cross_encoder_provider_models`](reference/tools/system/fetch_cross_encoder_provider_models)

### Update a provider
`system__update_cross_encoder_provider` replaces the whole row: pass the id and the full entity.

```json
{"toolset_id": "system", "tool_name": "update_cross_encoder_provider", "arguments": {"id": "ce-1", "entity": {"id": "ce-1", "provider": "huggingface", "models": [{"name": "BAAI/bge-reranker-v2-m3"}], "config": {"token": "hf-REPLACE"}, "limits": {"max_concurrency": 4}}}}
```

Reference: [`system__update_cross_encoder_provider`](reference/tools/system/update_cross_encoder_provider)

### Find providers
`system__find_cross_encoder_providers` filters rows by field values.

```json
{"toolset_id": "system", "tool_name": "find_cross_encoder_providers", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "ce-1"}}}}
```

Reference: [`system__find_cross_encoder_providers`](reference/tools/system/find_cross_encoder_providers)

### Get a provider
`system__get_cross_encoder_provider` fetches one row by id, the token masked.

```json
{"toolset_id": "system", "tool_name": "get_cross_encoder_provider", "arguments": {"id": "ce-1"}}
```

Reference: [`system__get_cross_encoder_provider`](reference/tools/system/get_cross_encoder_provider)

### Delete a provider
`system__delete_cross_encoder_provider` removes the row and drops its cached adapter at once.

```json
{"toolset_id": "system", "tool_name": "delete_cross_encoder_provider", "arguments": {"id": "ce-1"}}
```

Reference: [`system__delete_cross_encoder_provider`](reference/tools/system/delete_cross_encoder_provider)

### List providers
`system__list_cross_encoder_providers` browses the rows as a page.

```json
{"toolset_id": "system", "tool_name": "list_cross_encoder_providers", "arguments": {"limit": 20}}
```

Reference: [`system__list_cross_encoder_providers`](reference/tools/system/list_cross_encoder_providers)

## Gotchas
- Every tool on this page needs an admin; a run below the floor is a `forbidden`.
- The reserved id `huggingface-ce` is the local reranker bootstrap seeds: it cannot be created (409) or deleted (403).
- `huggingface` is the only kind; a draft that names any other provider answers `ok: false` with an `invalid draft` error.
- Deleting a provider a collection's search block names leaves the block pointing at nothing: update the collection first.

## Related
[embedding providers](providers/embedding), [knowledge search](knowledge/search).
