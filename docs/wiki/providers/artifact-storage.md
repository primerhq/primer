---
title: "Artifact storage"
summary: "Admin: choose where files and large outputs are stored, and probe a store before using it."
audience: admin
order: 6
tools: [system__list_artifact_storage_providers, system__get_artifact_storage_provider, system__create_artifact_storage_provider, system__update_artifact_storage_provider, system__delete_artifact_storage_provider, system__find_artifact_storage_providers, system__test_artifact_storage_provider, system__get_active_artifact_store, system__set_active_artifact_store]
related: [workspaces/what-a-workspace-is]
---
## What it is
An `ArtifactStorageProvider` row is a store for attachment bytes and large outputs. New bytes go to the deployment's active store; every artifact records where its bytes were written, and a read or a delete follows that record, so an artifact stays readable after a switch. Every tool on this page needs an admin.

## Concepts
- `provider` is `db`, `filesystem` or `s3`, and `config` must match it. `db` has no fields: the bytes live in the deployment's own storage. `filesystem` takes a required `root` directory. `s3` takes a required `bucket` plus optional `prefix`, `endpoint_url`, `region`, `addressing_style` (`auto`, `path` or `virtual`) and `access_key` and `secret_key`, which come back masked on every read.
- On this release the factory builds the `db` and `filesystem` backends. An `s3` row can be stored, but building it fails with `not implemented` until its backend ships; the console labels that kind "not implemented yet: stored, never used".
- A `filesystem` `root` is the absolute path of ONE folder that every server pod (the API and every worker) mounts at the same path. Creating the row, or changing its root, creates the folder when missing and writes a `.primer-artifact-root` marker in it; an update that keeps the root only checks that this server sees it with its marker. A root that is relative, is a file, cannot be written, or is an existing folder that already holds files and has no marker is a `validation-error` whose message starts `config.root: `, and nothing is stored. A process that finds no folder, or a folder without the marker, refuses the store with a message that names the root and says to mount the shared volume.
- The reserved row `artifact-storage-default` is the database store seeded at bootstrap. It is the store every artifact without a location record is read from, so its `provider` must stay `db`: a write that makes it another kind is a `validation-error`, and its delete is a 403.
- The active store is a deployment-wide setting, read on every write and never cached, so a switch is seen by every process at once. `system__get_active_artifact_store` reads it; `system__set_active_artifact_store` probes a saved provider and selects it for new writes. Existing artifacts stay where they were written.
- The `id` is optional on create: the server assigns `artifact-storage-provider-<hex>`, and it is immutable after creation.
- `system__test_artifact_storage_provider` runs a live round trip on a saved row: build, initialise, write a small file, read it back, delete it. It answers `{ok: true}` or `{ok: false, step, error}`, with the store's keys redacted out of the error. It prepares nothing: a filesystem root that has lost its marker fails at `initialize`.

## How to
1. Register the store: `db` needs nothing, `filesystem` the absolute path of a folder every server pod mounts, `s3` a bucket and, for a non-AWS endpoint, its `endpoint_url`.
2. Test it before selecting it.
3. Select it for new writes, and read back the selection to confirm.

## Tool calls
### Create a provider
`system__create_artifact_storage_provider` registers a store. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "create_artifact_storage_provider", "arguments": {"entity": {"id": "fs-1", "provider": "filesystem", "config": {"root": "/var/lib/primer/artifacts"}}}}
```

An `s3` row, with the keys masked on read:

```json
{"toolset_id": "system", "tool_name": "create_artifact_storage_provider", "arguments": {"entity": {"id": "s3-1", "provider": "s3", "config": {"bucket": "primer-artifacts", "prefix": "outputs/", "access_key": "AKIA-REPLACE", "secret_key": "REPLACE"}}}}
```

Reference: [`system__create_artifact_storage_provider`](reference/tools/system/create_artifact_storage_provider)

### Test a provider
`system__test_artifact_storage_provider` probes a saved row with a live round trip.

```json
{"toolset_id": "system", "tool_name": "test_artifact_storage_provider", "arguments": {"id": "fs-1"}}
```

Reference: [`system__test_artifact_storage_provider`](reference/tools/system/test_artifact_storage_provider)

### Get the active store
`system__get_active_artifact_store` reads which store new writes go to.

```json
{"toolset_id": "system", "tool_name": "get_active_artifact_store", "arguments": {}}
```

Reference: [`system__get_active_artifact_store`](reference/tools/system/get_active_artifact_store)

### Set the active store
`system__set_active_artifact_store` probes the named provider and selects it for new writes; existing artifacts stay where they were written.

```json
{"toolset_id": "system", "tool_name": "set_active_artifact_store", "arguments": {"provider_id": "fs-1"}}
```

Reference: [`system__set_active_artifact_store`](reference/tools/system/set_active_artifact_store)

### Get a provider
`system__get_artifact_storage_provider` fetches one row by id, the keys masked.

```json
{"toolset_id": "system", "tool_name": "get_artifact_storage_provider", "arguments": {"id": "fs-1"}}
```

Reference: [`system__get_artifact_storage_provider`](reference/tools/system/get_artifact_storage_provider)

### Update a provider
`system__update_artifact_storage_provider` replaces the whole row: pass the id and the full entity.

```json
{"toolset_id": "system", "tool_name": "update_artifact_storage_provider", "arguments": {"id": "fs-1", "entity": {"id": "fs-1", "provider": "filesystem", "config": {"root": "/var/lib/primer/artifacts"}}}}
```

Reference: [`system__update_artifact_storage_provider`](reference/tools/system/update_artifact_storage_provider)

### Find providers
`system__find_artifact_storage_providers` filters rows by field values.

```json
{"toolset_id": "system", "tool_name": "find_artifact_storage_providers", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "provider"}, "op": "=", "right": {"kind": "value", "value": "db"}}}}
```

Reference: [`system__find_artifact_storage_providers`](reference/tools/system/find_artifact_storage_providers)

### Delete a provider
`system__delete_artifact_storage_provider` removes the row; refused while the store is the active store or still holds artifacts.

```json
{"toolset_id": "system", "tool_name": "delete_artifact_storage_provider", "arguments": {"id": "fs-1"}}
```

Reference: [`system__delete_artifact_storage_provider`](reference/tools/system/delete_artifact_storage_provider)

### List providers
`system__list_artifact_storage_providers` browses the rows as a page.

```json
{"toolset_id": "system", "tool_name": "list_artifact_storage_providers", "arguments": {"limit": 20}}
```

Reference: [`system__list_artifact_storage_providers`](reference/tools/system/list_artifact_storage_providers)

## Gotchas
- Every tool on this page needs an admin; a run below the floor is a `forbidden`.
- `db` and `filesystem` build on this release: an `s3` row stores, but its probe and its selection as the active store fail at the build step with `not implemented` until the backend ships.
- A filesystem store works only when every server pod mounts the same volume at its `root`. A pod without it refuses the store, and at boot each pod logs every filesystem store it cannot use. Select a filesystem store only on a deployment whose pods all share that volume.
- `system__set_active_artifact_store` probes first: an unknown or unusable provider is a `validation-error`, and the current selection is unchanged.
- Deleting the active store is refused with `cascade_blocked`: select another store first. Deleting a store that still holds artifacts is refused with `artifact_provider_holds_artifacts`: the artifacts are read from it, and the row can go once they are gone.
- Moving a store that holds artifacts is refused the same way: changing its kind, or its `config.root`, `bucket` or `prefix`, would leave the stored artifacts unreadable. Register a new provider and select it instead.
- The reserved row `artifact-storage-default` cannot be deleted (403), and its `provider` must stay `db`.

## Related
[what a workspace is](workspaces/what-a-workspace-is).
