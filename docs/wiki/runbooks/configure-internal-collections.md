---
title: Enable the internal collections subsystem
summary: "Turn on semantic search over the platform's own document corpus: choose or verify the embedding provider, write the config, bootstrap, verify."
audience: admin
tools: [internal_collections__get_config, internal_collections__set_config, internal_collections__delete_config, internal_collections__bootstrap, internal_collections__bootstrap_status, system__get_embedding_provider, system__check_huggingface_models, system__test_embedding_provider, system__create_embedding_provider, system__get_semantic_search_provider, system__ask_user]
related: [purpose-map, providers/embedding, providers/embedding-model-options, providers/semantic-search, runbooks/conventions, not-agent-callable]
---

## Goal
"Enable the internal collections subsystem": the platform's own wiki store (the system collection) runs semantic search - an embedding provider vectorises its documents into a search provider, and queries search them.

## Preconditions
- the subsystem's current state is read first (the flow is idempotent; a configured subsystem reads back its config):
  - check: `internal_collections__get_config` ([reference](reference/tools/internal_collections/get_config)); expected: `{"configured": false}` when off, or the config row when on (if on, this runbook is already done - read [Verification](#verification) to prove the state).
- an embedding provider exists whose model will vectorise the corpus:
  - a clean install seeds the reserved local embedder: provider id `huggingface`, model `BAAI/bge-small-en-v1.5` (see [providers/embedding-model-options](providers/embedding-model-options) for the measured option set).
  - check: `system__get_embedding_provider` ([reference](reference/tools/system/get_embedding_provider)) on the provider id; expected: the row, with the intended model in `models`.
  - the user names a model instead: check it on the Hub with `system__check_huggingface_models` ([reference](reference/tools/system/check_huggingface_models)), then test the draft with `system__test_embedding_provider` ([reference](reference/tools/system/test_embedding_provider)) and register it with `system__create_embedding_provider` ([reference](reference/tools/system/create_embedding_provider)) (provider `huggingface`, the model id in `models`).
  - if neither holds: stop and say which provider the flow needs - do not invent one.
- a semantic search provider exists to hold the vectors:
  - a clean install seeds the reserved `lance` SSP (a local backend; no database needed).
  - check: `system__get_semantic_search_provider` ([reference](reference/tools/system/get_semantic_search_provider)) on the SSP id; expected: the row.
  - the user wants a database backend (`pgvector`, `pgvectorscale`) instead: that is the sibling flow [runbooks/change-search-engine](runbooks/change-search-engine) - register the provider there, then continue here with its id.

## Roles and gates
- runner: `admin` (every step's tool carries `required_role="admin"`; the subsystem vectorises the platform's own corpus).
- gated steps (a fresh install ships; the live gate is read with `system__describe_tool` at run time): the three writes park a run at an admin's rank until an admin approves (the admin-tier default gate, `required`; decided by an admin only):

| the scoped tool | shipped gate | who approves | what the approval card shows |
|---|---|---|---|
| `internal_collections__set_config` | `required` | an admin | the vector-space choice: the embedding provider + model, the search provider, the reranker when named |
| `internal_collections__delete_config` | `required` | an admin | (no arguments) the deactivation itself: search disabled, the reserved collections dropped |
| `internal_collections__bootstrap` | `required` | an admin | (no arguments) the activation: the embedding pass over the system collection |

The two reads (`internal_collections__get_config`, `internal_collections__bootstrap_status`) ship ungated. A running install may have added or retuned gates; a gated step parks - a person decides - and the parked call is not retried.

## Steps
### 1. Choose the embedding model (no silent default)
Read the option set on [providers/embedding-model-options](providers/embedding-model-options) - three tiers spanning memory cost (small, mid, large) with the measured numbers, plus the escape hatch to any HuggingFace model. Present the set to the user with `system__ask_user` ([reference](reference/tools/system/ask_user)) and wait for the choice:

- a tier: its model id (the mid tier needs no registration - the seeded `huggingface` provider already carries `BAAI/bge-small-en-v1.5`);
- "just pick one": the small tier is the documented safe pick - confirm it with the user, the pick is never silent (OQ-2);
- a named model: the precondition's check -> test -> register path, then the provider + model ids it produced.

Expected: a chosen `embedding_provider_id` + `embedding_model` pair, and the user's confirmation of it.

### 2. Write the config
Call `internal_collections__set_config` ([reference](reference/tools/internal_collections/set_config)) with the chosen pair and the search provider:

```json
{"toolset_id": "internal_collections", "tool_name": "set_config", "arguments": {"embedding_provider_id": "huggingface", "embedding_model": "BAAI/bge-small-en-v1.5", "search_provider_id": "lance"}}
```

Expected: the config row (`activated_at` still `null` - activation happens at bootstrap). Gated: the turn parks here until an admin approves (the card shows the vector-space choice); do not retry while it is parked. A reranker goes in here as `"cross_encoder": {"provider_id": "<id>", "model": "<model>"}` when the user wants one (optional; it stays editable later without disturbing the vectors). Once the subsystem is active, the vector-space fields freeze: a `type=conflict` naming frozen fields means deactivate first (step 6 of the failure table), then re-configure and re-bootstrap.

### 3. Bootstrap
Call `internal_collections__bootstrap` ([reference](reference/tools/internal_collections/bootstrap)) - no arguments:

```json
{"toolset_id": "internal_collections", "tool_name": "bootstrap", "arguments": {}}
```

Expected: `{"status": "succeeded", "state": "ready", ...}`. Gated: the turn parks here until an admin approves; do not retry while it is parked. The call runs the embedding pass over the system collection's documents before it answers, so it can take from tens of seconds to minutes - wait for it rather than re-calling.

### 4. Verify
Two reads: the durable state, and the behavior.

- the state: `internal_collections__bootstrap_status` ([reference](reference/tools/internal_collections/bootstrap_status)) -> expected: `state: "ready"`, `documents_indexed == documents_total`, and the number greater than zero.
- the behavior: a semantic query the corpus must answer - a search of the system collection for a phrase from a known page (the [purpose-map](purpose-map) is one): the search surface for the system collection is `POST /v1/collections/{collection_id}/search` with the id `system`; no agent tool reaches that surface, so the operator states the query and the admin applies it, or the flow reports the state read as its verification and names this as the human step (under [not-agent-callable](not-agent-callable)). Expected: hits > 0 including a page the phrase only appears in.

### 5. Report
State the verified end state to the user: the model the corpus runs on, the document count, and the one call that re-activates it after a deactivation (`internal_collections__bootstrap` again, after `set_config` if the pair changed).

## Verification
- `internal_collections__get_config` returns the row the flow wrote (provider, model, search provider match the user's choice).
- `internal_collections__bootstrap_status` reads `state: "ready"` with `documents_indexed == documents_total > 0`.
- the behavioral search (step 4) returned the known page.

## Refusal and failure
- `type=not-found` from `set_config` (the search provider id) -> the precondition's SSP check is the read that proves the id; register or name a real one.
- `type=not-found` from `bootstrap` -> no config row: run step 2 first.
- `type=conflict` naming frozen fields -> the subsystem is active on a different vector space: the only mutation path is `internal_collections__delete_config` ([reference](reference/tools/internal_collections/delete_config)) (deactivate: disables the search and drops the reserved collections), then `set_config` + `bootstrap` again.
- `status: "failed"` from `bootstrap` (or a `type=tool-error` naming the embedding model) -> the model is not one the provider permits, or the embedder did not answer: the precondition's `system__test_embedding_provider` is the read that isolates which.
- a search returns no hits after the state reads `ready` -> the query and the corpus use different spaces (a model changed under an old index): the fix is the deactivate -> re-configure -> bootstrap path above.
- `type=approval_required` -> parked; a person decides; do not retry.

## Related
- [providers/embedding-model-options](providers/embedding-model-options): the option set step 1 presents (and the escape hatch to any HuggingFace model).
- [providers/embedding](providers/embedding): the provider rows the embedder registers under.
- [runbooks/change-search-engine](runbooks/change-search-engine): the sibling flow for the vector store side.
- [purpose-map](purpose-map): the index row this runbook serves.
