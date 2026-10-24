---
title: "Embedding providers"
summary: "Admin: register the embedders that turn text into vectors for semantic search."
audience: admin
order: 3
tools: [system__list_embedding_providers, system__get_embedding_provider, system__create_embedding_provider, system__update_embedding_provider, system__delete_embedding_provider, system__find_embedding_providers, system__fetch_embedding_provider_models, system__check_huggingface_models, system__test_embedding_provider]
related: [providers/semantic-search, knowledge/collections]
---
## What it is
An `EmbeddingProvider` row is an embedder: it turns text into the vectors that semantic search runs on. Every tool on this page needs an admin.

## Concepts
- `provider` is `huggingface`, `openai` or `gemini`. Hugging Face runs local sentence-transformers models, downloaded on first use; its `config` is an optional `token`, needed only for gated repositories. `openai` targets any OpenAI-compatible endpoint: `config` carries a required `url`, an optional `api_key` and a `flavor` (`openai`, `lmstudio` or `other`). `gemini` carries an optional `api_key`.
- The row lists its `models`, at least one, each a `name`. The vector dimension is not on the row: the indexing pipeline learns it from the first chunk it embeds.
- A collection's search block names a provider and one of its models ([knowledge collections](knowledge/collections)); the block is validated against your configured providers at runtime.
- The embedding size is fixed per collection: a collection with data that is pointed at a model of another size answers 422 `dimension-mismatch` at index time.
- The id `huggingface` is reserved: bootstrap seeds the local embedder under it, a create with that id is a 409, and its delete is a 403.
- `limits` is the shape of the other provider families: a required `max_concurrency` plus the timeout and retry defaults.
- Two probes store nothing. `system__check_huggingface_models` checks Hub model names before you register them: one to 50 names, an optional token, and a per-model `ok` and `status`. `system__test_embedding_provider` probes a draft the way a saved row would: openai and gemini list the models their server offers, huggingface checks the named models on the Hub, and the answer is `{ok, models}` without a stored row.

## How to
1. For Hugging Face models, check the names on the Hub first, with the token when the repository is gated.
2. Test the draft when you are not sure it would work.
3. Create the provider with its models and limits.
4. Ask it which models it offers: the live list is what the server serves now.
5. Name the provider and a model in a collection's search block.

## Tool calls
### Check Hugging Face names
`system__check_huggingface_models` looks up Hub names before registration; it stores nothing.

```json
{"toolset_id": "system", "tool_name": "check_huggingface_models", "arguments": {"models": ["BAAI/bge-small-en-v1.5", "BAAI/bge-reranker-v2-m3"]}}
```

Reference: [`system__check_huggingface_models`](reference/tools/system/check_huggingface_models)

### Test a draft provider
`system__test_embedding_provider` probes the draft the way a saved row would; it stores nothing.

```json
{"toolset_id": "system", "tool_name": "test_embedding_provider", "arguments": {"provider": "openai", "config": {"url": "http://localhost:1234/v1"}}}
```

Reference: [`system__test_embedding_provider`](reference/tools/system/test_embedding_provider)

### Create a provider
`system__create_embedding_provider` registers the embedder with its models. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_embedding_provider", "arguments": {"entity": {"id": "hf-1", "provider": "huggingface", "models": [{"name": "all-MiniLM-L6-v2"}], "config": {"token": "hf-REPLACE"}, "limits": {"max_concurrency": 4}}}}
```

Reference: [`system__create_embedding_provider`](reference/tools/system/create_embedding_provider)

### Fetch the models it offers
`system__fetch_embedding_provider_models` lists what the server serves now; it differs from the row's permitted models.

```json
{"toolset_id": "system", "tool_name": "fetch_embedding_provider_models", "arguments": {"provider_id": "hf-1"}}
```

Reference: [`system__fetch_embedding_provider_models`](reference/tools/system/fetch_embedding_provider_models)

### Update a provider
`system__update_embedding_provider` replaces the whole row: pass the id and the full entity.

```json
{"toolset_id": "system", "tool_name": "update_embedding_provider", "arguments": {"id": "hf-1", "entity": {"id": "hf-1", "provider": "huggingface", "models": [{"name": "all-MiniLM-L6-v2"}], "config": {"token": "hf-REPLACE"}, "limits": {"max_concurrency": 4}}}}
```

Reference: [`system__update_embedding_provider`](reference/tools/system/update_embedding_provider)

### Find providers
`system__find_embedding_providers` filters rows by field values.

```json
{"toolset_id": "system", "tool_name": "find_embedding_providers", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "hf-1"}}}}
```

Reference: [`system__find_embedding_providers`](reference/tools/system/find_embedding_providers)

### Get a provider
`system__get_embedding_provider` fetches one row by id, the token masked.

```json
{"toolset_id": "system", "tool_name": "get_embedding_provider", "arguments": {"id": "hf-1"}}
```

Reference: [`system__get_embedding_provider`](reference/tools/system/get_embedding_provider)

### Delete a provider
`system__delete_embedding_provider` removes the row and drops its cached adapter at once.

```json
{"toolset_id": "system", "tool_name": "delete_embedding_provider", "arguments": {"id": "hf-1"}}
```

Reference: [`system__delete_embedding_provider`](reference/tools/system/delete_embedding_provider)

### List providers
`system__list_embedding_providers` browses the rows as a page.

```json
{"toolset_id": "system", "tool_name": "list_embedding_providers", "arguments": {"limit": 20}}
```

Reference: [`system__list_embedding_providers`](reference/tools/system/list_embedding_providers)

## Gotchas
- Every tool on this page needs an admin; a run below the floor is a `forbidden`.
- The reserved id `huggingface` is the local embedder bootstrap seeds: it cannot be created (409) or deleted (403).
- The dimension is learned once, from the first chunk. Pointing a collection with data at a model of another size is a 422 `dimension-mismatch`; the fix is to delete its documents, re-create the collection with the right embedder, and re-ingest.
- The `token` is write-only and masked on read; a mask sent back unchanged keeps the stored secret.
- Deleting a provider a collection's search block names leaves the block pointing at nothing: update the collection first.

## Related
[semantic search](providers/semantic-search), [knowledge collections](knowledge/collections).
