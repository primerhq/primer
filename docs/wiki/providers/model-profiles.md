---
title: "Model profiles"
summary: "Admin: a model profile names a provider, a model and a context length. Agents bind to profiles, and a profile can pool others."
audience: admin
order: 2
tools: [system__list_model_profiles, system__get_model_profile, system__create_model_profile, system__update_model_profile, system__delete_model_profile, system__find_model_profiles]
related: [providers/llm, agents/create-and-edit]
---
## What it is
A `ModelProfile` row is the registry of what an LLM provider serves: one row per provider, model and setting combination. An agent binds to a profile id, never to a provider and a model name. Every tool on this page needs an admin.

## Concepts
- A `single` profile (the default `kind`) carries `provider_id`, `model_name` (the provider-side wire name) and `context_length`. Several profiles may share a model name: that is how one model is offered with different settings, such as reasoning on and off.
- `context_length` is the maximum tokens the model accepts in a request; compaction reads it to decide when to summarise.
- `config` is applied to every call made under the profile: each call of an agent turn, each compaction call, and graph agent nodes that run on it. It holds `reasoning` (`off`, `minimal`, `low`, `medium` or `high`; absent keeps the vendor default) and `extended`, provider-specific knobs forwarded as-is. A refused setting never fails the call: the call is made once more without it, and the model runs without it from then on.
- An `aggregated` profile pools at least two single profiles. Its `kind` is `aggregated`, its `members` is the ordered list of member profile ids (the order is the failover chain), and its `provider_id`, `model_name` and `context_length` are null. Its settings: `strategy` (`sequential`, the default, or `round_robin`), `failover_point` (`before_first_token`, the default, or `mid_stream`) and `failover_on` (`transient_and_config`, the default, or `transient`). Its context window is the smallest member's, so a caller never overpromises.
- Each member's own config applies to the calls that member serves; the aggregate's own config is not applied. Failover is clean only before the first event: the default never re-emits tokens, and `mid_stream` may duplicate tokens already shown.
- Ids follow `<provider id>--<model name>` by convention only (`anthropic-1--claude-sonnet-4-6`); nothing enforces the shape.
- The list route adds usage per row: `agent_count` and `graph_node_count`.
- A row a harness manages (`harness_id` set) is changed by re-running the harness; direct CRUD on it is refused with a 409.

## How to
1. Create the provider first: [LLM providers](providers/llm).
2. Create one single profile per model you want to use, with the settings that model should run under.
3. Pool a model across providers with an aggregated profile when the pool is worth the trade-off.
4. Point agents at the profiles: [create and edit an agent](agents/create-and-edit). An agent create or update that names a profile that is not stored is refused.

## Tool calls
### Create a single profile
`system__create_model_profile` registers one model on one provider, with its context length and settings.

```json
{"toolset_id": "system", "tool_name": "create_model_profile", "arguments": {"entity": {"id": "anthropic-1--claude-sonnet-4-6", "description": "Sonnet with medium reasoning.", "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6", "context_length": 200000, "config": {"reasoning": "medium"}}}}
```

Reference: [`system__create_model_profile`](reference/tools/system/create_model_profile)

### Create an aggregated pool
Pool two profiles of the same model across providers; the member order is the failover chain.

```json
{"toolset_id": "system", "tool_name": "create_model_profile", "arguments": {"entity": {"id": "sonnet-pool", "description": "Sonnet on two providers; the first healthy one serves.", "kind": "aggregated", "members": ["anthropic-1--claude-sonnet-4-6", "openrouter-1--claude-sonnet-4-6"]}}}
```

Reference: [`system__create_model_profile`](reference/tools/system/create_model_profile)

### Update a profile
`system__update_model_profile` replaces the whole row; the semantic checks run again on the incoming shape.

```json
{"toolset_id": "system", "tool_name": "update_model_profile", "arguments": {"id": "anthropic-1--claude-sonnet-4-6", "entity": {"id": "anthropic-1--claude-sonnet-4-6", "description": "Sonnet with high reasoning.", "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6", "context_length": 200000, "config": {"reasoning": "high"}}}}
```

Reference: [`system__update_model_profile`](reference/tools/system/update_model_profile)

### Find profiles
`system__find_model_profiles` filters rows; handy for every profile a provider serves.

```json
{"toolset_id": "system", "tool_name": "find_model_profiles", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "provider_id"}, "op": "=", "right": {"kind": "value", "value": "anthropic-1"}}}}
```

Reference: [`system__find_model_profiles`](reference/tools/system/find_model_profiles)

### Get a profile
`system__get_model_profile` fetches one row by id.

```json
{"toolset_id": "system", "tool_name": "get_model_profile", "arguments": {"id": "anthropic-1--claude-sonnet-4-6"}}
```

Reference: [`system__get_model_profile`](reference/tools/system/get_model_profile)

### Delete a profile
`system__delete_model_profile` removes the row; refused while an agent uses it or an aggregate names it.

```json
{"toolset_id": "system", "tool_name": "delete_model_profile", "arguments": {"id": "anthropic-1--claude-sonnet-4-6"}}
```

Reference: [`system__delete_model_profile`](reference/tools/system/delete_model_profile)

### List profiles
`system__list_model_profiles` browses the rows; each item carries its usage counts.

```json
{"toolset_id": "system", "tool_name": "list_model_profiles", "arguments": {"limit": 20}}
```

Reference: [`system__list_model_profiles`](reference/tools/system/list_model_profiles)

## Gotchas
- Every tool on this page needs an admin; a run below the floor is a `forbidden`.
- A single profile whose `provider_id` names no stored provider is refused on create and on update (`provider_not_found`): create the provider first.
- Aggregated writes are checked at write time: at least two members, every member stored, every member single (nested aggregation is refused), no self-reference, no duplicate member. An update that turns a member of another aggregate into an aggregate itself is refused.
- Deleting a profile an agent uses, or that an aggregate names as a member, returns a 409; free the agent or remove the membership first.
- `reasoning` maps per provider and per model. A model with no such control gets none, and a refused level is retried once without the profile's settings.

## Related
[LLM providers](providers/llm), [create and edit an agent](agents/create-and-edit).
