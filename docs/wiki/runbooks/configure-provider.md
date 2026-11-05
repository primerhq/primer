---
title: Configure a provider
summary: Register an LLM provider, confirm its models, and make the model profile an agent, graph or trigger binds to; the on-ramp to any new model.
audience: admin
tools: [system__create_llm_provider, system__fetch_llm_provider_models, system__list_llm_providers, system__create_model_profile, system__list_model_profile_choices, system__update_agent]
related: [purpose-map, providers/llm, runbooks/conventions, not-agent-callable]
---

## Goal
"Configure a provider" end to end: a provider row the platform can call, its models confirmed reachable, and a model profile that names a model so an agent, graph or trigger can bind to it.

## Preconditions
- the target model is reachable: the endpoint (base URL and, where the backend needs it, the api key), the model name, and a rough context length.
  - check: none before step 1 (the provider row carries the connection; step 2 proves the models).
- where the provider row needs a key: the key is supplied by the user (a secret; the platform stores it masked and never echoes it back in full).

## Roles and gates
- runner: `admin` (platform configuration; the access model's own semantics: an admin configures the platform through the operator).
- gated steps (a fresh install ships; the live gate is read with `system__describe_tool` at run time):
  - no step in this flow ships a gate: `system__create_llm_provider` and `system__create_model_profile` ship ungated (the role gate is admin for the configuration writes). A running install may have added gates: step 0 says how to see them.

## Steps
### 0. See the live gates of the writes
Call `system__describe_tool` ([reference](reference/tools/system/describe_tool)) on each write you are about to make -> expected: the live approval gate (or none). If a live gate exists, the turn parks there until a person approves; that is expected, do not retry.

### 1. Register the provider
Call `system__create_llm_provider` ([reference](reference/tools/system/create_llm_provider)). The provider type and its config fields are documented on [providers/llm](providers/llm); the body:

```json
{"toolset_id": "system", "tool_name": "create_llm_provider", "arguments": {"id": "<provider id>", "provider": "<the backend type>", "config": {"<the backend's connection fields: url and api_key where the type carries them>"}, "limits": {"max_concurrency": 1}}}
```

Expected: the row. `limits.max_concurrency` 1 is the right setting for a single-request backend (a local model server); raise it for a gateway that takes load.

### 2. Confirm what models it serves
Call `system__fetch_llm_provider_models` ([reference](reference/tools/system/fetch_llm_provider_models)) on the provider -> expected: the model names the endpoint actually publishes. The model name the profile will use (step 3) must be in this list; a name the endpoint does not publish fails at call time, not at create time.

### 3. Make the model profile
Call `system__create_model_profile` ([reference](reference/tools/system/create_model_profile)):

```json
{"toolset_id": "system", "tool_name": "create_model_profile", "arguments": {"id": "<profile id>", "description": "<what the model is for>", "provider_id": "<the provider id>", "model_name": "<the model name from step 2>", "context_length": <the context length>}}
```

Expected: the profile row. A profile is the unit an agent, graph or trigger binds to (its `model.profile_id`).

### 4. Bind the profile where it is needed
For the agent that should run on the model, call `system__update_agent` ([reference](reference/tools/system/update_agent)) setting `model` to `{"profile_id": "<the profile id>"}` (the agent row's other fields ride: read the row first, send the whole row with the model changed).

## Verification
- `system__list_llm_providers` lists the provider; `system__list_model_profile_choices` lists the new profile as a choice an agent can bind.
- the agent's row (read with the tool that reads agent rows) shows `model.profile_id` = the new profile.
- a run of the bound agent answers (the behavioral check): the first turn completes without a provider error.

## Refusal and failure
- type=validation-error (the provider config) -> the config does not match the provider type's shape (a `url` on a type that hard-codes its base URL, an api key on a keyless backend): read [providers/llm](providers/llm) for the type's fields and retry with the corrected body.
- type=validation-error (the profile) -> the `provider_id` names no provider row, or the `model_name` is empty; the provider row is the read that proves the id.
- a run answers with a provider connectivity error -> the endpoint is down or the key is wrong: the provider row's probe fields (`last_probe_at`, `last_probe_ok`, `last_error`) show the last real probe; re-fetch the models (step 2) to distinguish a dead endpoint from a bad model name.
- type=approval_required -> a live gate the step-0 read named: parked; a person decides; do not retry.

## Related
- [purpose-map](purpose-map): the index row this runbook serves.
- [providers/llm](providers/llm): the provider types and their config shapes.
- [runbooks/change-search-engine](runbooks/change-search-engine): the sibling configuration (the semantic search provider).
- the embedding provider and the internal collections flow are the sibling how-tos of this one: pending (M2).
