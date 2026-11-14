---
title: Enable collection search
summary: Check provider use, approve the inline backfill, prove semantic hits and deliberately disable the index.
audience: admin
tools: [system__get_collection, system__list_embedding_providers, system__list_semantic_search_providers, system__describe_tool, collections_api__enable_collection_search, collections_api__get_collection_search_status, collections_api__disable_collection_search, collections__search]
related: [knowledge/search, knowledge/collections, runbooks/ingest-a-knowledge-base, runbooks/change-search-engine]
---
## Goal

Enable semantic search for an existing collection, prove a real indexed hit, and disable vectors while retaining the documents when needed.

## Preconditions

- Run as an authenticated `user` who owns the collection, or as an admin. Full access alone does not permit enabling or disabling search; read access is enough to query and inspect status.
- Read [`system__get_collection`](reference/tools/system/get_collection) for `team-knowledge`. It must be a collection you can read; confirm ownership and that it contains nonempty text. If it does not exist, follow [ingest a knowledge base](runbooks/ingest-a-knowledge-base). The worked ids are used only after these reads confirm them; carry your returned ids through every call.

```json
{"toolset_id": "system", "tool_name": "get_collection", "arguments": {"id": "team-knowledge"}}
```

- Prove the source text exists before backfilling: use the ungated nonsemantic read below and require a hit for your stored Cedar text. With no hit, stop and [ingest the source](runbooks/ingest-a-knowledge-base) first.

```json
{"toolset_id": "collections", "tool_name": "search", "arguments": {"collection": "team-knowledge", "query": "Cedar", "mode": "literal", "limit": 5}}
```

- An admin checks [`system__list_embedding_providers`](reference/tools/system/list_embedding_providers) and [`system__list_semantic_search_providers`](reference/tools/system/list_semantic_search_providers). These reads require `admin`; a user must ask an administrator for the usable provider ids and exact permitted embedding model, rather than trying to bypass the role floor. Confirm that your account has use permission on both providers. The worked configuration uses `team-embedder`, its model `team-embedding`, and `team-ssp` only when those are the confirmed usable choices. Missing embedding/model configuration is a stop: [configure a provider](runbooks/configure-provider). Missing vector backend or connectivity is a stop: [change the search engine](runbooks/change-search-engine). Do not write a collection search block to guess providers.

```json
{"toolset_id": "system", "tool_name": "list_embedding_providers", "arguments": {"limit": 200}}
```

```json
{"toolset_id": "system", "tool_name": "list_semantic_search_providers", "arguments": {"limit": 200}}
```

- Inspect the live gates with [`system__describe_tool`](reference/tools/system/describe_tool) before either write. Operators can change the shipped policy and approvers.

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "collections_api", "tool_name": "enable_collection_search"}}
```

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "collections_api", "tool_name": "disable_collection_search"}}
```

## Roles and gates

Runner: `user` with owner access; provider setup is an admin precondition. An approval permits a particular call and preserves its parked caller; it does not lend the approver's role or ownership.

| write | shipped gate | who decides and what to check |
|---|---|---|
| [`collections_api__enable_collection_search`](reference/tools/collections_api/enable_collection_search) | required | the stamped live approver or an admin; verify collection id and the provider/model/chunking configuration on the card |
| [`collections_api__disable_collection_search`](reference/tools/collections_api/disable_collection_search) | required | the stamped live approver or an admin; verify the collection id before dropping vectors |

The read [`collections_api__get_collection_search_status`](reference/tools/collections_api/get_collection_search_status) and [`collections__search`](reference/tools/collections/search) are ungated. Inside a worker turn a required write parks until a person decides; do not retry it while parked. An unapproved call performs no write. The native MCP v2 boundary can deliberately report only `tool_unavailable`; read the live gate first instead of treating an opaque refusal as permission. Rejection leaves that requested effect unapplied. Approval still cannot authorize a full-access non-owner to change search.

## Steps

1. Enable the existing collection with [`collections_api__enable_collection_search`](reference/tools/collections_api/enable_collection_search). The turn parks here for approval; do not retry. This is an inline backfill of existing text, not a background-job submission. State and error are lifecycle-owned, not client-set fields.

```json
{"toolset_id": "collections_api", "tool_name": "enable_collection_search", "arguments": {"collection_id": "team-knowledge", "body": {"embedder": {"provider_id": "team-embedder", "model": "team-embedding"}, "vector_store_provider_id": "team-ssp", "cross_encoder": null, "chunking": {"max_chars": 1500, "overlap": 200}}}}
```

   The returned collection must have `search.state=ready` and no search error. A successful HTTP response carrying `search.state=error` is a failed backfill, not completion. On an indexing failure, keep the documents, diagnose the error and fix the named provider; a new approved call with the same configuration retries indexing. Never issue that retry while the first turn is parked. Replacing the embedder/model or vector provider drops the old namespace before backfill; choose the new configuration deliberately.

2. Read status using [`collections_api__get_collection_search_status`](reference/tools/collections_api/get_collection_search_status). Expect `state=ready`; inspect `documents_total`, `documents_indexed` and `error`. Empty folder or section bodies need not produce vectors, so counts alone are not proof of useful retrieval.

```json
{"toolset_id": "collections_api", "tool_name": "get_collection_search_status", "arguments": {"collection_id": "team-knowledge"}}
```

3. Prove a hit for text you actually ingested with [`collections__search`](reference/tools/collections/search). Use explicit `semantic`, not `auto`, so a lexical fallback cannot masquerade as a working vector index. The ingestion worked example contains the Cedar on-call text.

```json
{"toolset_id": "collections", "tool_name": "search", "arguments": {"collection": "team-knowledge", "query": "Cedar on-call escalation", "mode": "semantic", "limit": 5}}
```

   Require `mode_used=semantic` and a hit whose path and excerpt match that stored text. A made-up answer, an empty hit list or a fulltext/literal result does not prove this flow.

4. To deliberately turn vectors off, use [`collections_api__disable_collection_search`](reference/tools/collections_api/disable_collection_search). This separately parks for approval; do not retry. Its REST result is no content (204). It drops vectors and clears the search block, even if the old vector provider is missing; it does not delete document bodies.

```json
{"toolset_id": "collections_api", "tool_name": "disable_collection_search", "arguments": {"collection_id": "team-knowledge"}}
```

## Verification

Reread the collection and the live gate with the same exact reads above. After enable, require ready status and a semantic hit from durable text. After deliberate disable, the status read must say `disabled` and the collection's `search` must be null. An explicit semantic query must now refuse with the available modes named; documents remain searchable through the nonsemantic modes.

```json
{"toolset_id": "collections", "tool_name": "search", "arguments": {"collection": "team-knowledge", "query": "Cedar", "mode": "literal", "limit": 5}}
```

## Refusal and failure

| result | meaning and next move |
|---|---|
| `type=approval_required` or a parked approval | a person decides; do not retry or treat it as a completed backfill |
| native MCP `tool_unavailable` | opaque role/exposure/gate refusal; verify the live tool/gate and ask an authorized operator, do not bypass it |
| rejected approval | no permission to perform this call; report refusal and verify search state is unchanged |
| forbidden / HTTP 403 | the caller lacks owner access, or tried an admin-only provider read; approval does not change this |
| not found / HTTP 404 | collection is absent or hidden; ask its owner rather than probing hidden rows |
| conflict / HTTP 409 naming an unregistered provider | missing and unusable providers are intentionally indistinguishable; ask an admin to check registration and your use grant |
| validation error / HTTP 422 | invalid request fields; use the generated reference schema and the confirmed configuration |
| `search.state=error`, even with HTTP 200 | backfill failed or was interrupted; fix its recorded cause, then request a fresh approved retry |
| semantic mode unavailable | search is disabled or not ready; inspect status, not a fallback result |

## Related

- [knowledge/search](knowledge/search): modes and semantic retrieval.
- [knowledge/collections](knowledge/collections): ownership and optional search.
- [runbooks/ingest-a-knowledge-base](runbooks/ingest-a-knowledge-base): write and reread the source text.
- [runbooks/change-search-engine](runbooks/change-search-engine): provision the vector backend.
