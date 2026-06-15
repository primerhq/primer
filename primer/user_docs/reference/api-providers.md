---
slug: api-providers
title: Providers API
section: reference
summary: REST endpoints for LLM, embedding, cross-encoder, and semantic-search providers, including model listing, cache invalidation, and secret handling.
---

Providers are the backend adapters that agents and knowledge features use. There are four provider families: LLM providers (for chat/completion), embedding providers (for vector search), cross-encoder providers (for re-ranking), and semantic-search providers (pgvector, pgvectorscale, Lance).

```ref:toolsets/toolsets-system
How providers relate to tools and toolsets.
```

```ref:features/agents
Configure an agent to use a provider.
```

## Provider families and base paths

| Family | Base path | Supported backends |
|--------|-----------|--------------------|
| LLM | `/v1/llm_providers` | `anthropic`, `openresponses`, `openchat`, `gemini`, `ollama`, `openrouter` |
| Embedding | `/v1/embedding_providers` | `huggingface`, `openai`, `gemini` |
| Cross-encoder | `/v1/cross_encoder_providers` | `huggingface` |
| Semantic search | `/v1/ssp` | `pgvector`, `pgvectorscale`, `lance` |

## LLM providers

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/llm_providers` | List LLM providers |
| POST | `/v1/llm_providers` | Create an LLM provider |
| GET | `/v1/llm_providers/{id}` | Get LLM provider by id |
| PUT | `/v1/llm_providers/{id}` | Replace (full update) |
| DELETE | `/v1/llm_providers/{id}` | Delete |
| POST | `/v1/llm_providers/find` | Filter by predicate |
| GET | `/v1/llm_providers/{id}/models` | List configured model names (row-cached) |
| POST | `/v1/llm_providers/{id}/invalidate` | Drop the cached adapter |
| POST | `/v1/llm_providers/_discover_models` | Probe a draft config for its model list |

### LLM provider object

```json
{
  "id": "anthropic-prod",
  "provider": "anthropic",
  "models": [
    {"name": "claude-sonnet-4-6", "context_length": 200000}
  ],
  "config": {
    "api_key": "***"
  },
  "limits": {
    "max_concurrency": 4
  }
}
```

**Note on secrets:** `config.api_key` (and equivalent fields for other providers) is **never returned in plaintext**. The GET and LIST responses replace the value with a masked string. The field is always present (not omitted) so you can confirm a key was stored.

| Field | Required | Description |
|-------|----------|-------------|
| `id` | no | Identifier (case-sensitive). If omitted, the server assigns a type-prefixed id (e.g. `llm-provider-3f9a1c8d`). Immutable after creation |
| `provider` | yes | Provider type: one of `anthropic`, `openresponses`, `openchat`, `gemini`, `ollama`, `openrouter` |
| `models` | yes | Non-empty list of `{"name": string, "context_length": integer}` entries |
| `config` | yes | Provider-specific connection config (discriminated by `provider`). API keys are accepted in write requests but masked on read. |
| `limits.max_concurrency` | yes | Max simultaneous in-flight requests (integer > 0) |
| `limits.request_timeout_seconds` | no | Per-event inactivity timeout for streaming calls (float, default 300.0). If no event arrives from the provider within this window the stream is aborted and the turn fails cleanly. Set to `null` to disable. See [LLM stream timeout](#llm-stream-timeout) for guidance. |

### LLM stream timeout

`limits.request_timeout_seconds` is a per-event inactivity timeout, not a total-generation cap. A long but progressing response is not killed; only a complete stall (no new event arriving) triggers it. The default 300 s covers most real-world runs. For local servers such as LM Studio on slower hardware you can lower this (e.g. 60 s) for faster failure detection; raise or disable (`null`) for very large models that have long gaps between tokens.

### Create an LLM provider

`POST /v1/llm_providers` - returns `201 Created`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/llm_providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "anthropic-prod",
    "provider": "anthropic",
    "models": [{"name": "claude-sonnet-4-6", "context_length": 200000}],
    "config": {"api_key": "sk-ant-..."},
    "limits": {"max_concurrency": 4}
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/llm_providers",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "anthropic-prod",
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200000}],
        "config": {"api_key": "sk-ant-..."},
        "limits": {"max_concurrency": 4},
    },
)
assert r.status_code == 201
--- javascript
const r = await fetch("/v1/llm_providers", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "anthropic-prod",
    provider: "anthropic",
    models: [{name: "claude-sonnet-4-6", context_length: 200000}],
    config: {api_key: "sk-ant-..."},
    limits: {max_concurrency: 4}
  })
})
```

**Errors:** `422` if `models` is empty or config shape does not match `provider`. `409` on duplicate `id`.

### Get configured model names

`GET /v1/llm_providers/{id}/models` returns the model names stored in the provider row. This endpoint is **row-cached** - it never calls the upstream LLM. It reflects the `models` list you configured, not a live discovery call.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/llm_providers/anthropic-prod/models \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/llm_providers/anthropic-prod/models",
              headers={"Authorization": f"Bearer {token}"})
print(r.json())  # {"models": ["claude-sonnet-4-6"]}
--- javascript
const r = await fetch("/v1/llm_providers/anthropic-prod/models", {
  headers: {"Authorization": `Bearer ${token}`}
})
const {models} = await r.json()
```

Response `200 OK`:

```json
{"models": ["claude-sonnet-4-6"]}
```

**Errors:** `404` if the provider id does not exist.

### Invalidate cached adapter

`POST /v1/llm_providers/{id}/invalidate` drops the in-memory adapter for the given provider. Call this after a `PUT` to ensure the next request picks up the updated config. Returns `204 No Content` - including when the id does not exist (no-op).

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/llm_providers/anthropic-prod/invalidate \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.post("https://your-host/v1/llm_providers/anthropic-prod/invalidate",
               headers={"Authorization": f"Bearer {token}"})
assert r.status_code == 204
--- javascript
await fetch("/v1/llm_providers/anthropic-prod/invalidate", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Embedding providers

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/embedding_providers` | List embedding providers |
| POST | `/v1/embedding_providers` | Create an embedding provider |
| GET | `/v1/embedding_providers/{id}` | Get by id |
| PUT | `/v1/embedding_providers/{id}` | Replace |
| DELETE | `/v1/embedding_providers/{id}` | Delete |
| POST | `/v1/embedding_providers/find` | Filter by predicate |
| GET | `/v1/embedding_providers/{id}/models` | List configured model names (row-cached) |
| POST | `/v1/embedding_providers/{id}/invalidate` | Drop cached embedder adapter |
| POST | `/v1/embedding_providers/_discover_models` | Probe a draft config for its model list |

### Embedding provider object

```json
{
  "id": "hf-embedder",
  "provider": "huggingface",
  "models": [
    {"name": "sentence-transformers/all-MiniLM-L6-v2"},
    {"name": "sentence-transformers/all-mpnet-base-v2"}
  ],
  "config": {"token": "***"},
  "limits": {"max_concurrency": 2}
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `id` | no | Identifier (case-sensitive). If omitted, the server assigns a type-prefixed id (e.g. `embedding-provider-3f9a1c8d`). Immutable after creation |
| `provider` | yes | One of `huggingface`, `openai`, `gemini` |
| `models` | yes | Non-empty list of `{"name": string}` entries (no `dim` field at the row level) |
| `config` | yes | Provider-specific config (API token/key masked on read) |
| `limits.max_concurrency` | yes | Max in-flight requests |

### Create an embedding provider

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/embedding_providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "hf-embedder",
    "provider": "huggingface",
    "models": [{"name": "sentence-transformers/all-MiniLM-L6-v2"}],
    "config": {"token": "hf-..."},
    "limits": {"max_concurrency": 2}
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/embedding_providers",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "hf-embedder",
        "provider": "huggingface",
        "models": [{"name": "sentence-transformers/all-MiniLM-L6-v2"}],
        "config": {"token": "hf-..."},
        "limits": {"max_concurrency": 2},
    },
)
--- javascript
await fetch("/v1/embedding_providers", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "hf-embedder",
    provider: "huggingface",
    models: [{name: "sentence-transformers/all-MiniLM-L6-v2"}],
    config: {token: "hf-..."},
    limits: {max_concurrency: 2}
  })
})
```

The `/models` and `/invalidate` sub-resources work identically to the LLM provider equivalents. After a `PUT`, call `/invalidate` to force the runtime to reload the updated config.

## Cross-encoder providers

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/cross_encoder_providers` | List cross-encoder providers |
| POST | `/v1/cross_encoder_providers` | Create |
| GET | `/v1/cross_encoder_providers/{id}` | Get by id |
| PUT | `/v1/cross_encoder_providers/{id}` | Replace |
| DELETE | `/v1/cross_encoder_providers/{id}` | Delete |
| POST | `/v1/cross_encoder_providers/find` | Filter by predicate |
| GET | `/v1/cross_encoder_providers/{id}/models` | List configured model names |
| POST | `/v1/cross_encoder_providers/{id}/invalidate` | Drop cached adapter |

Only the `huggingface` backend is supported. Object shape follows the same pattern as embedding providers (`id`, `provider`, `models`, `config`, `limits`). The `id` is optional on create: omit it and the server assigns a type-prefixed id (e.g. `cross-encoder-provider-3f9a1c8d`); it is immutable after creation.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/cross_encoder_providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "hf-reranker",
    "provider": "huggingface",
    "models": [{"name": "cross-encoder/ms-marco-MiniLM-L-6-v2"}],
    "config": {"token": "hf-..."},
    "limits": {"max_concurrency": 1}
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/cross_encoder_providers",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "hf-reranker",
        "provider": "huggingface",
        "models": [{"name": "cross-encoder/ms-marco-MiniLM-L-6-v2"}],
        "config": {"token": "hf-..."},
        "limits": {"max_concurrency": 1},
    },
)
--- javascript
await fetch("/v1/cross_encoder_providers", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "hf-reranker",
    provider: "huggingface",
    models: [{name: "cross-encoder/ms-marco-MiniLM-L-6-v2"}],
    config: {token: "hf-..."},
    limits: {max_concurrency: 1}
  })
})
```

## Semantic-search providers

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/ssp` | List semantic-search providers |
| POST | `/v1/ssp` | Create |
| GET | `/v1/ssp/{id}` | Get by id |
| PUT | `/v1/ssp/{id}` | Replace |
| DELETE | `/v1/ssp/{id}` | Delete |
| POST | `/v1/ssp/find` | Filter by predicate |
| POST | `/v1/ssp/{id}/invalidate` | Drop cached adapter |

Supported `provider` values: `pgvector`, `pgvectorscale`, `lance`. The `id` is optional on create: omit it and the server assigns a type-prefixed id (e.g. `semantic-search-provider-3f9a1c8d`); it is immutable after creation.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/ssp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "pgvec-main",
    "provider": "pgvector",
    "config": {"url": "postgresql://user:pass@db:5432/primer"}
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/ssp",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "pgvec-main",
        "provider": "pgvector",
        "config": {"url": "postgresql://user:pass@db:5432/primer"},
    },
)
--- javascript
await fetch("/v1/ssp", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "pgvec-main",
    provider: "pgvector",
    config: {url: "postgresql://user:pass@db:5432/primer"}
  })
})
```

## Secret handling

API keys and tokens submitted in `config` are stored encrypted. GET and LIST responses always include the `api_key` (or equivalent) field but **never return the plaintext value** - the field contains a masked string. This applies to all provider families.

## Errors note

All error responses use the RFC 7807 `ProblemDetails` envelope. Common status codes:

- `422` - validation failed (e.g. `models` list is empty, or `config` shape does not match `provider`)
- `409` - duplicate `id`
- `404` - provider not found (also returned by `GET /models`; note that `POST /invalidate` returns `204` even for missing ids)
- `204` - successful delete or invalidate (no body)
