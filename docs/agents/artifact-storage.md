---
slug: artifact-storage
title: Artifact storage - where message media and service files are kept
summary: Where primer keeps message media and service bundle files (the database, a shared folder or an S3 bucket), how to register, test and select the store new files go to over MCP, and why older files stay readable after a switch. Every tool here needs an admin.
related: [channels, sessions, mcp-exposure, model-providers]
mcp_tools:
  - system::list_artifact_storage_providers
  - system::create_artifact_storage_provider
  - system::update_artifact_storage_provider
  - system::delete_artifact_storage_provider
  - system::test_artifact_storage_provider
  - system::get_active_artifact_store
  - system::set_active_artifact_store
---

# Artifact storage

## Overview

Artifacts are the files primer keeps for you: media that arrives on a channel (Slack, Telegram, Discord), the workspace files a person attaches to a message sent to a session, the workspace files an agent attaches to an `inform_user` or `ask_user` prompt, and the files of published service bundles. A message carries the artifact's id, never its bytes. They are stored by an **artifact storage provider**, a row in `artifact_storage_providers` whose `provider` is `db` (the primer database), `filesystem` (a folder every server pod shares) or `s3` (a bucket on AWS S3 or an S3-compatible server such as MinIO).

Every tool on this page needs an admin: a caller without the admin role is refused before the tool runs.

- The rows are listed, read, created, edited and deleted with the generic `system::list_artifact_storage_providers`, `system::get_artifact_storage_provider`, `system::find_artifact_storage_providers`, `system::create_artifact_storage_provider`, `system::update_artifact_storage_provider` and `system::delete_artifact_storage_provider`.
- `system::test_artifact_storage_provider` tries a SAVED store with a live round trip (build it, write a small probe file, read it back, delete it) and says which step failed.
- `system::get_active_artifact_store` says which store new files go to.
- `system::set_active_artifact_store` tests a store the same way, then selects it for new files.

## Mental model

- **The active store.** New files go to the store the active setting names. With no setting, that is the reserved `artifact-storage-default` row, the database. The setting is read on every write and never cached, so a switch is seen at once everywhere.
- **A location record per file.** A store the database cannot serve (filesystem, S3) gets a record of which provider holds each file it stores. Reading and deleting a file follow its record, even after the active store changed, so older files stay readable where they were written.
- **The database fallback.** A file with no record is a database file, read straight from the database. That is why the reserved `artifact-storage-default` row must stay `provider: db`: a create or update of it that names another kind is a `validation-error`, and it cannot be deleted (`forbidden`).
- **Every kind is built.** `db`, `filesystem` and `s3` stores can be tested and selected. An `s3` store needs the server's `s3` extra (aiobotocore): without it, its test fails at the `build` step and names the extra to install.
- **A filesystem root is one shared folder.** Its `root` is the absolute path of a folder that every server pod (the API and every worker) mounts at the same path. Creating the row, or changing its root, creates the folder when missing and writes a `.primer-artifact-root` marker in it; an update that keeps the root only checks it. A root that cannot be prepared (or checked), or an existing folder that already holds files and has no marker, is a `validation-error` naming `config.root`, and nothing is stored. A pod that finds no folder, or one without the marker, refuses the store and says to mount the shared volume.
- **An S3 store is a bucket and a key prefix.** Each file is one object at `<prefix><file id>`, in AWS S3 or an S3-compatible server (set `endpoint_url` for one; leave it blank for AWS; a blank `region` is `us-east-1`). `addressing_style` is `auto` (path-style when `endpoint_url` is set, as MinIO needs; AWS's own choice otherwise), `path` or `virtual`. The endpoint and the region come only from the row, never from the server's environment.
- **S3 keys: both or neither.** Set `access_key` and `secret_key` together, or leave both blank to use the server's own credentials (its environment, its AWS config files or its instance or pod role). One without the other is a `validation-error` naming the blank key, and nothing is stored; a half pair is never completed from the server's own credentials. The keys come back masked (`**********`) on every read; to keep them on an update, send the masks back unchanged.
- **A store that holds files cannot move or go away.** Deleting the active store, deleting a store that still holds files, and changing the `provider`, a filesystem `root` or an S3 `bucket` or `prefix` of a store that holds files are refused with `type=conflict`. Select another store first, and register a new provider rather than moving a used one. A `db` store other than the reserved one holds no records, so it can be deleted whenever it is not active.

## Lifecycle

1. **Register** a store with `system::create_artifact_storage_provider`. A `filesystem` row's folder is prepared now (created when missing, given its marker) by the server that handles the call, so the shared volume must already be mounted on every pod.
2. **Test** it with `system::test_artifact_storage_provider`. A store that fails says at which step (`build`, `initialize`, `put`, `get` or `delete`) and why.
3. **Select** it with `system::set_active_artifact_store`. The store is tested again first; only a store that passes is selected. From then on new files go to it; files written earlier stay where they were and are still read from there.
4. **Switch again** whenever you like: selecting a store moves nothing.
5. **Retire** a store: select another one, and delete the row once the files it holds are gone. While it holds files, its delete is refused with the count.

The same operations exist over REST: `POST /v1/artifact_storage_providers/{id}/_test` for a saved row, `POST /v1/artifact_storage_providers/_test` for a draft that is not saved, and `GET` / `PUT /v1/artifact_storage_active_config`.

## MCP tools

| Tool | Use it for |
|------|------------|
| `system::list_artifact_storage_providers` | Browsing the registered stores (needs an admin). |
| `system::create_artifact_storage_provider` | Registering a store; a filesystem root is prepared before the row is stored (needs an admin). |
| `system::update_artifact_storage_provider` | Replacing a store's row; send the key masks back to keep the keys (needs an admin). |
| `system::delete_artifact_storage_provider` | Removing a store that is not active and holds no files (needs an admin). |
| `system::test_artifact_storage_provider` | Checking a saved store end to end before you select it (needs an admin). |
| `system::get_active_artifact_store` | Reading which store new files go to (needs an admin). |
| `system::set_active_artifact_store` | Sending new files to another saved store, after it passes the same test (needs an admin). |

## Workflows

### Register a filesystem store, test it and select it

The folder must be the mount path of a volume that every server pod shares. Register the row:

```json
{"tool": "system::create_artifact_storage_provider", "arguments": {"entity": {"id": "fs-media", "provider": "filesystem", "config": {"root": "/var/lib/primer/artifacts"}}}}
```

```json
{"id": "fs-media", "provider": "filesystem", "config": {"root": "/var/lib/primer/artifacts"}}
```

Test it:

```json
{"tool": "system::test_artifact_storage_provider", "arguments": {"id": "fs-media"}}
```

```json
{"ok": true}
```

Select it for new files:

```json
{"tool": "system::set_active_artifact_store", "arguments": {"provider_id": "fs-media"}}
```

```json
{"provider_id": "fs-media", "provider": "filesystem"}
```

Read the selection back:

```json
{"tool": "system::get_active_artifact_store", "arguments": {}}
```

```json
{"provider_id": "fs-media", "provider": "filesystem"}
```

### Register an S3 store on a MinIO server

```json
{"tool": "system::create_artifact_storage_provider", "arguments": {"entity": {"id": "s3-media", "provider": "s3", "config": {"bucket": "primer-artifacts", "prefix": "media/", "endpoint_url": "http://minio:9000", "addressing_style": "path", "access_key": "REPLACE", "secret_key": "REPLACE"}}}}
```

The keys come back masked:

```json
{"id": "s3-media", "provider": "s3", "config": {"bucket": "primer-artifacts", "prefix": "media/", "endpoint_url": "http://minio:9000", "region": null, "addressing_style": "path", "access_key": "**********", "secret_key": "**********"}}
```

The same row with only `access_key` set is refused, and nothing is stored:

```json
{"type": "validation-error", "message": "config.secret_key: set both access_key and secret_key, or neither to use the server's own credentials"}
```

Then test it and select it as above.

### When a test or a selection fails

A `filesystem` row whose folder this server does not see (the shared volume is not mounted):

```json
{"ok": false, "step": "initialize", "error": "ConfigError: artifact root '/var/lib/primer/artifacts' does not exist in this process; mount the shared artifact volume at this path in every server pod (the API and every worker)"}
```

An `s3` row whose bucket does not exist on its server:

```json
{"ok": false, "step": "put", "error": "ClientError: An error occurred (NoSuchBucket) when calling the PutObject operation: The specified bucket does not exist"}
```

Selecting a store that fails its test leaves the selection unchanged:

```json
{"type": "validation-error", "message": "provider_id: artifact store 'fs-media' failed its check at initialize: ConfigError: artifact root '/var/lib/primer/artifacts' does not exist in this process; mount the shared artifact volume at this path in every server pod (the API and every worker)"}
```

### Retire a store

Deleting the active store is refused:

```json
{"tool": "system::delete_artifact_storage_provider", "arguments": {"id": "fs-media"}}
```

```json
{"type": "conflict", "message": "currently the active artifact store; select another store first"}
```

Once another store is active, a store that still holds files is refused with the count:

```json
{"type": "conflict", "message": "artifact provider 'fs-media' still holds 1 artifact(s), and they are read from it; it can be deleted once they are gone"}
```

Moving it is refused the same way:

```json
{"tool": "system::update_artifact_storage_provider", "arguments": {"id": "fs-media", "entity": {"id": "fs-media", "provider": "filesystem", "config": {"root": "/var/lib/primer/artifacts-2"}}}}
```

```json
{"type": "conflict", "message": "config.root: artifact provider 'fs-media' holds 1 artifact(s) at its current location; changing config.root would leave them unreadable. Register a new provider and select it as the active store instead"}
```

## Gotchas

- **These tools need an admin.** A run started by a user without the admin role cannot call them.
- **A filesystem store needs the shared volume in every pod.** The API and every worker must mount the same volume at `root`. Selecting tests the store in one server process only; a pod without the volume refuses the store at its next write, and at boot every pod logs each filesystem store it cannot use. Select a filesystem store only on a deployment whose pods all share that volume.
- **`prefix + id` is literal.** The object key is the prefix followed directly by the file id: `media/` gives `media/artifact-...`, while `media` gives `mediaartifact-...`. End the prefix with `/` to use a folder.
- **S3 keys: both or neither.** One key alone is a `validation-error`. A row stored that way before the rule fails its test at `build`.
- **Ambient credentials are each pod's own.** An S3 row with no keys signs with whatever credentials each server pod has (environment, AWS config files, instance or pod role), so every pod needs them, and the `s3` extra. The slim and full images have the extra; a source install adds it with `pip install 'primer-ai[s3]'` and a restart.
- **A new `endpoint_url` needs the keys again.** An update that changes an S3 row's `endpoint_url` and sends the key masks back is a `validation-error` (re-enter the secret): the stored keys are kept only for the same origin. An `endpoint_url` or `region` may be corrected on a store that holds files, as long as it reaches the same bucket.
- **The two conflicts.** A delete is refused while the store is active or holds files; an update is refused when it moves a store that holds files (`provider`, `root`, `bucket`, `prefix`). Both are `type=conflict`.
- **The reserved row stays `db`.** `artifact-storage-default` cannot become another kind (`validation-error`) or be deleted (`forbidden`). To store new files elsewhere, register another provider and select it.
- **A selection can lose a race, and then nothing changes.** The test can take seconds. If the store is deleted while it is being tested, or someone else selects ANOTHER store meanwhile, the answer is `type=conflict` and the selection stays as it was: read it again with `system::get_active_artifact_store` and decide. Someone selecting the same store meanwhile is no conflict: the answer is the selection.
- **An upload can lose the same race.** A file being written to a store that is deleted (or moved) meanwhile fails with a conflict and is not kept; sending it again puts it in the store that is active now.
- **Files stay where they were written.** Selecting a store changes where NEW files go; it moves nothing.

## Related

- [channels](channels.md): channel media is stored as artifacts.
- [sessions](sessions.md): files attached to session messages and to `inform_user` / `ask_user` prompts.
- [mcp-exposure](mcp-exposure.md): calling these system tools over MCP.
- [model-providers](model-providers.md): the other provider rows an admin registers.
