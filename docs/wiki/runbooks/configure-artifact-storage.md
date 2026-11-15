---
title: Configure artifact storage
summary: Register and probe a provider, select it for new bytes, prove old and new reads, then restore and clean only the owned sample.
audience: admin
tools: [system__create_artifact_storage_provider, system__test_artifact_storage_provider, system__set_active_artifact_store, system__get_active_artifact_store, system__list_artifact_storage_providers, system__get_artifact_storage_provider, system__delete_artifact_storage_provider, system__describe_tool]
related: [providers/artifact-storage, runbooks/configure-provider, not-agent-callable]
---
## Goal

Select a proven artifact store for new bytes while keeping existing artifacts readable and the default DB provider intact.

## Preconditions

Runner: admin. Confirm capacity, backups and access to the actual destination. For filesystem storage, every API and worker process must mount the same volume at the same absolute root path. For S3, confirm the bucket, prefix, endpoint and credentials privately; a served secret mask is not a credential. Stop if the operator cannot verify mounts or credentials. Do not turn a missing mount into an empty local directory.

Read [`system__get_active_artifact_store`](reference/tools/system/get_active_artifact_store), [`system__list_artifact_storage_providers`](reference/tools/system/list_artifact_storage_providers) and [`system__get_artifact_storage_provider`](reference/tools/system/get_artifact_storage_provider). Record the original provider_id and retain artifact-storage-default as DB. These are reads, before any write.

```json
{"toolset_id": "system", "tool_name": "get_active_artifact_store", "arguments": {}}
```

```json
{"toolset_id": "system", "tool_name": "list_artifact_storage_providers", "arguments": {"limit": 20}}
```

```json
{"toolset_id": "system", "tool_name": "get_artifact_storage_provider", "arguments": {"id": "artifact-storage-default"}}
```

Read live policy with [`system__describe_tool`](reference/tools/system/describe_tool) for each write. A default policy is not a guarantee that a live call is ungated.

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "set_active_artifact_store"}}
```

## Roles and gates

| operation | shipped gate | authority |
|---|---|---|
| create provider, test provider, select active provider | none | admin; selection probes again and can refuse |
| get/list providers, read active store | none | admin; ordinary entity sharing does not grant platform configuration |
| delete owned provider | none | admin; active references and held bytes block deletion |

An ordinary user must be refused these tools. If a live gate parks, wait for its eligible approver; do not use a twin or retry to bypass it. Approval never makes an unusable destination valid.

## Steps

1. Create a named provider with [`system__create_artifact_storage_provider`](reference/tools/system/create_artifact_storage_provider). This filesystem example requires the confirmed shared mount. Creation prepares a new root and its .primer-artifact-root marker; it does not select the provider. Updating an unchanged root checks rather than recreates a missing marker.

```json
{"toolset_id": "system", "tool_name": "create_artifact_storage_provider", "arguments": {"entity": {"id": "team-artifacts", "provider": "filesystem", "config": {"root": "/srv/primer-artifacts"}}}}
```

For S3 instead, use provider=s3 and its current config: bucket and optional prefix, endpoint_url, region, addressing_style, and both access_key/secret_key or neither for ambient credentials. Do not log credentials. The local filesystem sample does not prove an external S3 deployment.

2. Read the saved provider and use [`system__test_artifact_storage_provider`](reference/tools/system/test_artifact_storage_provider) before activation. Require ok=true; a failing step or redacted error is a stop. The probe performs initialize/put/read/delete and does not activate.

```json
{"toolset_id": "system", "tool_name": "get_artifact_storage_provider", "arguments": {"id": "team-artifacts"}}
```

```json
{"toolset_id": "system", "tool_name": "test_artifact_storage_provider", "arguments": {"id": "team-artifacts"}}
```

3. Select the inspected id with [`system__set_active_artifact_store`](reference/tools/system/set_active_artifact_store). This re-probes and commits only if still valid. Unknown/unusable/raced destinations must leave the prior selection unchanged. Require the returned provider_id and provider type, then reread active selection.

```json
{"toolset_id": "system", "tool_name": "set_active_artifact_store", "arguments": {"provider_id": "team-artifacts"}}
```

```json
{"toolset_id": "system", "tool_name": "get_active_artifact_store", "arguments": {}}
```

## Verification

Require both actual new bytes and old bytes to read back byte-for-byte. The active pointer chooses new writes; it does not migrate historical bytes. Existing artifact locations retain their provider. A probe or a successful selection alone does not prove this behavior or mounts across all worker pods.

Use the normal attachment/media flow or the human [service publication surface](not-agent-callable) to publish and read a small disposable artifact before and after the switch. Record actual content/digests and verify the old artifact after selecting the new provider, then the new artifact after restoring the original provider. There is no generic artifact-upload tool promised here. Do not manufacture an unlisted endpoint or inspect the database to infer success.

For deliberate rollback select the original provider_id recorded in Preconditions. The worked original is the default DB provider; use the actual captured id on a real deployment.

```json
{"toolset_id": "system", "tool_name": "set_active_artifact_store", "arguments": {"provider_id": "artifact-storage-default"}}
```

Delete the sample through the surface that owns its bytes, verify those bytes are gone, then use [`system__delete_artifact_storage_provider`](reference/tools/system/delete_artifact_storage_provider) only on the owned inactive provider. Read/list must no longer contain it. Preserve the reserved DB provider, original active selection, unrelated artifacts and default workspace.

```json
{"toolset_id": "system", "tool_name": "delete_artifact_storage_provider", "arguments": {"id": "team-artifacts"}}
```

The delete acknowledgement names deleted=true and the exact id; reread/list must confirm absence.

## Refusal and failure

- Non-admin/role/exposure refusal: stop; a read grant or approval is not platform authority.
- Failed probe, missing root marker, inaccessible mount/bucket, invalid credential or a redacted failing step: leave the active selection unchanged and ask the operator to repair the real destination. Do not recreate a missing marker to conceal a lost mount.
- Concurrent provider edit/delete or selection change: reread both current provider and active pointer and make a deliberate new request; do not overwrite another operator's choice blindly.
- cascade_blocked: the provider is active. Restore a proven destination first. artifact_provider_holds_artifacts: old bytes still reference it; deletion or changing its kind/root/bucket/prefix would strand them. Do not force deletion, rename the destination or remove files by hand.
- A broken old/new read or failed sample cleanup is not completion. Restore the original active pointer where safe, report the exact failure and retain the referenced provider for recovery. Selection is not migration, and this flow makes no zero-downtime or cross-pod mount guarantee.

## Related

[Artifact providers](providers/artifact-storage), [provider setup](runbooks/configure-provider), [human and REST surfaces](not-agent-callable).
