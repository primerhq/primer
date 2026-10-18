---
slug: model-providers
title: Model providers - register LLM, embedding and cross-encoder providers, check them first, import models as profiles
summary: How to register an LLM, embedding or cross-encoder provider over MCP, check Hugging Face models on the Hub and test a draft before saving it, and turn an LLM provider's models into profiles named provider--model.
related: [agents, web-tools]
mcp_tools:
  - system::create_llm_provider
  - system::create_model_profile
  - system::create_embedding_provider
  - system::create_cross_encoder_provider
  - system::check_huggingface_models
  - system::test_embedding_provider
  - system::test_cross_encoder_provider
  - system::fetch_llm_provider_models
---

# Model providers

## Overview

A model provider is a connection primer makes to something that serves models. There are three families, each with its own create tool:

- **LLM providers** (the language models agents run on): `system::create_llm_provider`. Kinds: `openresponses`, `openchat`, `anthropic`, `gemini`, `ollama`, `openrouter`.
- **Embedding providers** (vectors for semantic search): `system::create_embedding_provider`. Kinds: `openai` (any OpenAI-compatible server), `gemini`, `huggingface`.
- **Cross-encoder providers** (rerankers): `system::create_cross_encoder_provider`. One kind: `huggingface`.

Registering a provider is two steps for an LLM and one for the other families: an LLM provider is only a connection, and each model it serves becomes a **model profile** (`system::create_model_profile`); an embedding or cross-encoder provider carries its models on the row. Three tools let you look before you save: `system::check_huggingface_models` asks the Hugging Face Hub whether models exist and can be pulled, and `system::test_embedding_provider` and `system::test_cross_encoder_provider` test a draft provider that has not been created. Every tool in this doc needs the `admin` role, and none of the three look-before-you-save tools stores anything.

## Mental model

- An **LLM provider row** has `id`, `provider` (the kind), `config` (the connection: a `url`, an `api_key`, a `flavor` for OpenAI-compatible servers) and `limits` (`max_concurrency` is required). It has no `models`: what a provider serves is its profiles.
- A **model profile** is one named configuration of one model on one provider: `id`, `description` (required), `provider_id`, `model_name` (the name the server knows the model by) and `context_length` (a whole number of 1 or more, required). An agent does not name a provider and a model: it names a profile, `{"profile_id": "lmstudio--qwen3-8b"}` (see [agents](agents.md)).
- An **embedding provider row** has `provider`, `config`, `limits` and `models`, a list of at least one `{"name": ...}`. A **cross-encoder row** is the same with `models` of `{"name": ..., "max_pair_length"?: ...}`.
- A **Hugging Face** provider has no list of models to ask for: you name them, and primer pulls them from the Hub (or the mirror the deployment points `HF_ENDPOINT` at) the first time they are used. A typo, a gated repository or a refused token would otherwise show up at the first embed, long after the provider was saved, which is what `system::check_huggingface_models` is for. The token is optional: public models need none.
- A **check or test answers, it does not fail**. `system::check_huggingface_models` returns one result per model; the two test tools return `{"ok": true, ...}` or `{"ok": false, "error": ...}`. A model the Hub refuses, a server that is down and a draft that cannot be a row are all answers, not tool errors.
- **Secrets are masked when you read a row back** (`**********` plus the last four characters of an API key or an embedding token; `**********` for a cross-encoder token). A masked value is not a key.

## MCP tools

| Tool | Use it for |
|------|------------|
| `system::check_huggingface_models` | Asking the Hub, before registering a Hugging Face provider, whether each model exists and the token can pull it (1 to 50 names). |
| `system::test_embedding_provider` | Trying a draft embedding provider: the models an `openai` or `gemini` server lists, or the Hub check of a `huggingface` draft. |
| `system::test_cross_encoder_provider` | Trying a draft cross-encoder provider: the Hub check of the models it names. |
| `system::create_embedding_provider` | Registering an embedding provider, models included. |
| `system::create_cross_encoder_provider` | Registering a cross-encoder provider, models included. |
| `system::create_llm_provider` | Registering an LLM connection. |
| `system::create_model_profile` | Registering one model of an LLM provider, so an agent can use it. |
| `system::fetch_llm_provider_models` | Not usable for an LLM provider today (see the last item of Gotchas). |

The same checks exist over REST for a client that is not an agent: `POST /v1/huggingface/_check_models`, `POST /v1/embedding_providers/_test` and `POST /v1/cross_encoder_providers/_test` take the same arguments and return the same bodies.

## Workflows

Check two Hugging Face models, then register the provider with the one that passed:

```json
{"tool": "system::check_huggingface_models", "arguments": {"models": ["BAAI/bge-small-en-v1.5", "BAAI/bge-small-en-v9"], "token": "hf_xxxxxxxx"}}
```

```json
{"models": [
  {"name": "BAAI/bge-small-en-v1.5", "ok": true, "status": "ok", "detail": "Can be pulled"},
  {"name": "BAAI/bge-small-en-v9", "ok": false, "status": "not_found", "detail": "Not found, or private and the token cannot see it"}
]}
```

The results come back in the order asked. Register the provider with the model that passed:

```json
{"tool": "system::create_embedding_provider", "arguments": {"entity": {"id": "hf-embed", "provider": "huggingface", "config": {"token": "hf_xxxxxxxx"}, "models": [{"name": "BAAI/bge-small-en-v1.5"}], "limits": {"max_concurrency": 2}}}}
```

The answer is the stored row with the token masked (`"token": "**********xxxx"`). A cross-encoder provider is the same with `system::create_cross_encoder_provider` and models such as `{"name": "BAAI/bge-reranker-v2-m3", "max_pair_length": 512}`.

Try a draft OpenAI-compatible embedding server before registering it (nothing is saved, and `id`, `limits` and `models` are not needed):

```json
{"tool": "system::test_embedding_provider", "arguments": {"provider": "openai", "config": {"url": "http://localhost:1234/v1"}}}
```

```json
{"ok": true, "models": ["text-embedding-nomic-embed-text-v1.5", "qwen3-8b"]}
```

An OpenAI-compatible server lists every model it serves, language models included: pick the embedding models. When the server cannot be reached the answer is `{"ok": false, "error": "..."}` with the reason, and a draft that cannot be a row says which field: `{"ok": false, "error": "invalid draft: 1 validation error for EmbeddingProvider\nurl\n  Field required [type=missing, input_type=dict]"}`. A Gemini draft (`"provider": "gemini"`) lists the models that can embed. A Hugging Face draft checks the models it names and says which failed:

```json
{"tool": "system::test_embedding_provider", "arguments": {"provider": "huggingface", "config": {}, "models": [{"name": "BAAI/bge-small-en-v1.5"}, {"name": "nope/missing"}]}}
```

```json
{"ok": false, "models": ["BAAI/bge-small-en-v1.5"], "checks": [
  {"name": "BAAI/bge-small-en-v1.5", "ok": true, "status": "ok", "detail": "Can be pulled"},
  {"name": "nope/missing", "ok": false, "status": "not_found", "detail": "Not found, or private and the token cannot see it"}
], "error": "1 of 2 models failed the Hub check: nope/missing (not_found)"}
```

Register an LLM provider, then one profile per model, each named `<provider>--<model>`:

```json
{"tool": "system::create_llm_provider", "arguments": {"entity": {"id": "lmstudio", "provider": "openchat", "config": {"url": "http://localhost:1234/v1", "flavor": "lmstudio"}, "limits": {"max_concurrency": 2}}}}
```

```json
{"tool": "system::create_model_profile", "arguments": {"entity": {"id": "lmstudio--qwen3-8b", "description": "Imported from lmstudio", "provider_id": "lmstudio", "model_name": "qwen3-8b", "context_length": 32768}}}
```

The answer is the stored profile (trimmed here):

```json
{"id": "lmstudio--qwen3-8b", "description": "Imported from lmstudio", "kind": "single", "provider_id": "lmstudio", "model_name": "qwen3-8b", "context_length": 32768}
```

An agent can now use `{"profile_id": "lmstudio--qwen3-8b"}` as its model.

## Gotchas

- **The profile id rule.** Build a profile id from the provider id and the model name joined with `--`, and keep it to `^[A-Za-z0-9][A-Za-z0-9._-]*$`: replace every other character with `-`, then collapse runs of `-` and trim `-`, `.` and `_` from the ends of each part. A model name such as `anthropic/claude-3.5:beta` on a provider `or` becomes `or--anthropic-claude-3.5-beta`; `llama3.1:8b` on `ollama-1` becomes `ollama-1--llama3.1-8b`. A `/` in a profile id makes the row impossible to read or delete by its URL, which is why it is replaced.
- **A `conflict` from `create_model_profile` (a 409 over REST) means the profile already exists.** That is not a failure of an import: the model is already registered. Skip it, or `system::update_model_profile` if you meant to change it.
- **`context_length` has to be a whole number of 1 or more, and the server may not tell you it.** Anthropic and the real OpenAI API report none. Get it from the operator or the model's card. A guess is worse than asking.
- **Tests and checks store nothing, and `ok: false` is an answer.** Read `error` and, for the Hub, each result's `status`: `ok`, `gated` (accept the model's terms on the Hub with the token's account), `not_found` (it does not exist, or it is private and the token cannot see it), `bad_token` (the Hub refused the token, or the token has a space or a line break and was not sent), `disabled`, `invalid_name` (not `namespace/name`; letters, digits, `.`, `_` and `-`, with no `..` or `--`) and `error` (the Hub could not be reached or answered something else).
- **The Hub is asked from the primer server**, using the deployment's `HF_ENDPOINT` (the public Hub unless an operator set a mirror). "Can be pulled" means the server's network and the token can pull it, which is what matters when the model loads.
- **Never test with a value you read back.** A stored key comes back masked (`**********`), and a test would send exactly that to the server. Pass the real key, or test the stored provider through its own routes.
- **An embedding or cross-encoder provider needs at least one model**, and a Hugging Face model that failed its check will fail the same way the first time it is used. Register the ones that passed. The ids `huggingface` (embedding) and `huggingface-ce` (cross encoder) belong to the built-in rows the platform seeds; creating a row with either id is refused.
- **`system::fetch_llm_provider_models` does not list an LLM provider's models.** An LLM adapter has no model list of its own (a provider's models are its profiles), so the call fails on every real provider. To see what a saved LLM provider's server offers, read `GET /v1/llm_providers/{id}/discovered_models`; for what is already registered, `GET /v1/llm_providers/{id}/models`. The embedding and cross-encoder variants return the model names configured on the row.

## Related

- [agents](agents.md) - an agent names a model profile, `{"profile_id": ...}`.
- [web-tools](web-tools.md) - the web search and fetch tools; their providers are a separate class of rows, tested with `POST /v1/web_search_providers/_test` and `POST /v1/web_fetch_providers/_test`.
