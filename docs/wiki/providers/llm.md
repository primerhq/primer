---
title: "LLM providers"
summary: "Admin: register the model servers agents talk to and list the models a provider offers."
audience: admin
order: 1
tools: [system__list_llm_providers, system__get_llm_provider, system__create_llm_provider, system__update_llm_provider, system__delete_llm_provider, system__find_llm_providers, system__fetch_llm_provider_models]
related: [providers/model-profiles, agents/create-and-edit]
---
## What it is
An `LLMProvider` row is a model server agents talk through: which backend, how to reach it, and the limits the server can take. Every tool on this page needs an admin. The row does not carry its models: what a provider serves is the set of [model profiles](providers/model-profiles) that point at it.

## Concepts
- `provider` names the backend: `openresponses`, `openchat`, `gemini`, `anthropic`, `ollama` or `openrouter`. `config` must match it. The two OpenAI-compatible backends take a `url`, an optional `api_key` and a `flavor` that names the server on the wire (`openai`, `lmstudio`, `ollama`, `vllm` or `other`). `gemini` and `anthropic` take an optional `api_key`. `ollama` takes a `url` and an optional `api_key`. `openrouter` takes a required `api_key` and optional `app_name` and `app_url`, and its base URL is fixed.
- `limits` budgets the server. `max_concurrency` is required, and a single-slot server (LM Studio) wants 1. The rest default: `request_timeout_seconds` 300, `max_retries` 2.
- The row carries probe state, not a model list: `last_probe_at`, `last_probe_ok` and `last_error`. `system__fetch_llm_provider_models` is the one tool that actually calls a stored provider, and it stamps these fields with the outcome.
- Secrets are write-only: an `api_key` (or a URL password) comes back masked on every read, and a mask sent back unchanged keeps the stored secret.
- The `id` is optional on create: the server assigns `llm-provider-<hex>`, and it is immutable after creation.

## How to
1. Create the provider row with its config and limits.
2. Ask it which models it serves: the live probe answers with the upstream catalogue.
3. Register what you want to use: one [model profile](providers/model-profiles) per model, each pointing at the provider.
4. Point agents at the profiles, never at the provider alone: [create and edit an agent](agents/create-and-edit).

## Tool calls
### Create a provider
`system__create_llm_provider` registers the server. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_llm_provider", "arguments": {"entity": {"id": "anthropic-1", "provider": "anthropic", "config": {"api_key": "sk-ant-REPLACE"}, "limits": {"max_concurrency": 4}}}}
```

Reference: [`system__create_llm_provider`](reference/tools/system/create_llm_provider)

### Fetch the models it serves
`system__fetch_llm_provider_models` probes the upstream with the stored config and stamps the probe fields.

```json
{"toolset_id": "system", "tool_name": "fetch_llm_provider_models", "arguments": {"provider_id": "anthropic-1"}}
```

Reference: [`system__fetch_llm_provider_models`](reference/tools/system/fetch_llm_provider_models)

### Update a provider
`system__update_llm_provider` replaces the whole row: pass the id and the full entity.

```json
{"toolset_id": "system", "tool_name": "update_llm_provider", "arguments": {"id": "anthropic-1", "entity": {"id": "anthropic-1", "provider": "anthropic", "config": {"api_key": "sk-ant-REPLACE"}, "limits": {"max_concurrency": 8}}}}
```

Reference: [`system__update_llm_provider`](reference/tools/system/update_llm_provider)

### Find providers
`system__find_llm_providers` filters rows by field values.

```json
{"toolset_id": "system", "tool_name": "find_llm_providers", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "provider"}, "op": "=", "right": {"kind": "value", "value": "anthropic"}}}}
```

Reference: [`system__find_llm_providers`](reference/tools/system/find_llm_providers)

### Get a provider
`system__get_llm_provider` fetches one row by id, secrets masked.

```json
{"toolset_id": "system", "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}}
```

Reference: [`system__get_llm_provider`](reference/tools/system/get_llm_provider)

### Delete a provider
`system__delete_llm_provider` removes the row and drops its cached adapter at once.

```json
{"toolset_id": "system", "tool_name": "delete_llm_provider", "arguments": {"id": "anthropic-1"}}
```

Reference: [`system__delete_llm_provider`](reference/tools/system/delete_llm_provider)

### List providers
`system__list_llm_providers` browses the rows as a page.

```json
{"toolset_id": "system", "tool_name": "list_llm_providers", "arguments": {"limit": 20}}
```

Reference: [`system__list_llm_providers`](reference/tools/system/list_llm_providers)

## Gotchas
- Every tool on this page needs an admin; a run below the floor is a `forbidden`.
- Deleting a provider does not check its model profiles: the profiles stay, and they start failing at turn time, when the agent's profile is resolved. Delete or re-point the profiles first.
- A masked secret sent back unchanged keeps the stored value, but only when the row's origin (scheme, host, port) is unchanged. Change the URL and the update is refused: re-enter the secret.
- A config shaped for another backend is a `validation-error`. `openrouter` refuses a `url` outright, and a config that does not match `provider` names the field.
- A failed probe writes `last_error` with the row's own key scrubbed out of the message.

## Related
[model profiles](providers/model-profiles), [create and edit an agent](agents/create-and-edit).
