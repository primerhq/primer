---
slug: artifact-storage
title: Artifact storage - test a store, read and select the store new files go to
summary: How artifact storage providers route files (the active store for new files, a location record per file, the database fallback), and how to test a saved store, read which one is active and select another over MCP. Every tool here needs an admin.
related: [model-providers, workspaces]
mcp_tools:
  - system::test_artifact_storage_provider
  - system::get_active_artifact_store
  - system::set_active_artifact_store
---

# Artifact storage

## Overview

Artifacts are the files primer keeps for you: images and files attached to messages (a session's, or a channel's), files an agent sends with `inform_user`, files a person sends back through an `ask_user` form, and published service bundles. They are stored by an **artifact storage provider**, a row in `artifact_storage_providers` whose `provider` is `db` (the primer database), `filesystem` or `s3`.

Three tools operate the stores. All three need an admin: a caller without the admin role is refused before the tool runs.

- `system::test_artifact_storage_provider` (needs an admin): tries a SAVED store with a live round trip (build it, write a small probe file, read it back, delete it) and says which step failed.
- `system::get_active_artifact_store` (needs an admin): which store new files go to.
- `system::set_active_artifact_store` (needs an admin): tests a store the same way, then selects it for new files.

The rows themselves are created, edited and deleted with the generic `system::create_artifact_storage_provider`, `system::update_artifact_storage_provider` and `system::delete_artifact_storage_provider` tools.

## Mental model

- **The active store.** New files go to the store the active setting names. With no setting, that is the reserved `artifact-storage-default` row, the database. The setting is read on every write and never cached, so a switch is seen at once everywhere.
- **A location record per file.** A store the database cannot serve (filesystem, S3) gets a record of which provider holds each file it stores. Reading and deleting a file follow its record, even after the active store changed, so older files stay readable where they were written.
- **The database fallback.** A file with no record is a database file, read straight from the database. That is why the reserved `artifact-storage-default` row must stay `provider: db`: a create or update of it that names another kind is a `validation-error`.
- **Every kind is built.** `db`, `filesystem` and `s3` stores can be tested and selected. An `s3` store needs the server's `s3` extra (aiobotocore): without it, its test fails at the `build` step and names the extra to install.
- **An S3 store is a bucket and a key prefix.** Each file is one object at `<prefix><file id>`, in AWS S3 or an S3-compatible server such as MinIO (set `endpoint_url` for one; leave it blank for AWS). End the prefix with `/` to use a folder. `addressing_style` `auto` uses path-style requests when `endpoint_url` is set, as MinIO needs.
- **S3 keys: both or neither.** Set `access_key` and `secret_key` together, or leave both blank to use the server's own credentials (its environment, its AWS config files or its instance or pod role). One without the other is a `validation-error` naming the blank key, and nothing is stored; a half pair is never completed from the server's own credentials. The keys come back masked on every read; to keep them on an update, send the masks back unchanged.
- **A filesystem root is one shared folder.** Its `root` is the absolute path of a folder that every server pod (the API and every worker) mounts at the same path. Creating the row, or changing its root, creates the folder when missing and writes a `.primer-artifact-root` marker in it; an update that keeps the root only checks it. A root that cannot be prepared (or checked), or an existing folder that already holds files and has no marker, is a `validation-error` naming `config.root`, and nothing is stored. A pod that finds no folder, or one without the marker, refuses the store and says to mount the shared volume.
- **A store that holds files cannot move or go away.** Deleting the active store, deleting a store that still holds files, and changing the `provider`, a filesystem `root` or an S3 `bucket` or `prefix` of a store that holds files are refused with `type=conflict`. Select another store first, and register a new provider rather than moving a used one. (An S3 `endpoint_url` or `region` may be corrected on a store that holds files; it must still reach the same bucket.)

## MCP tools

| Tool | Use it for |
|------|------------|
| `system::test_artifact_storage_provider` | Checking a saved store end to end before you select it (needs an admin). |
| `system::get_active_artifact_store` | Reading which store new files go to (needs an admin). |
| `system::set_active_artifact_store` | Sending new files to another saved store, after it passes the same test (needs an admin). |

The same operations exist over REST: `POST /v1/artifact_storage_providers/{id}/_test` for a saved row, `POST /v1/artifact_storage_providers/_test` for a draft that is not saved, and `GET` / `PUT /v1/artifact_storage_active_config`.

## Workflows

Test a saved store:

```json
{"tool": "system::test_artifact_storage_provider", "arguments": {"id": "db-2"}}
```

```json
{"ok": true}
```

A store that fails says at which step (`build`, `initialize`, `put`, `get` or `delete`) and why, with any key in the text redacted. A `filesystem` row whose folder this server does not see (the shared volume is not mounted):

```json
{"ok": false, "step": "initialize", "error": "ConfigError: artifact root '/var/lib/primer/artifacts' does not exist in this process; mount the shared artifact volume at this path in every server pod (the API and every worker)"}
```

An `s3` row whose bucket does not exist on its server:

```json
{"ok": false, "step": "put", "error": "ClientError: An error occurred (NoSuchBucket) when calling the PutObject operation: The specified bucket does not exist"}
```

Register an S3 store on a MinIO server (the keys come back masked):

```json
{"tool": "system::create_artifact_storage_provider", "arguments": {"entity": {"id": "s3-media", "provider": "s3", "config": {"bucket": "primer-artifacts", "prefix": "media/", "endpoint_url": "http://minio:9000", "access_key": "REPLACE", "secret_key": "REPLACE"}}}}
```

The same row with only `access_key` set is refused, and nothing is stored:

```json
{"type": "validation-error", "message": "config.secret_key: set both access_key and secret_key, or neither to use the server's own credentials"}
```

Read the active store:

```json
{"tool": "system::get_active_artifact_store", "arguments": {}}
```

```json
{"provider_id": "artifact-storage-default", "provider": "db"}
```

Select another store for new files. It is tested first, and only a store that passes is selected:

```json
{"tool": "system::set_active_artifact_store", "arguments": {"provider_id": "db-2"}}
```

```json
{"provider_id": "db-2", "provider": "db"}
```

## Gotchas

- **These tools need an admin.** A run started by a user without the admin role cannot call them.
- **Selecting tests first.** An unknown id, or a store that fails its test, is a `validation-error` that names `provider_id` (and the failing step), and the selection is unchanged.
- **A selection can lose a race, and then nothing changes.** The test can take seconds. If the store is deleted while it is being tested, or someone else selects ANOTHER store meanwhile, the answer is `type=conflict` and the selection stays as it was: read it again with `system::get_active_artifact_store` and decide. Someone selecting the same store meanwhile is no conflict: the answer is the selection.
- **An upload can lose the same race.** A file being written to a store that is deleted meanwhile fails with a conflict and is not kept; sending it again puts it in the store that is active now.
- **An S3 store needs the `s3` extra in every server pod.** The slim and full images have it; a source install adds it with `pip install 'primer-ai[s3]'` and a restart.
- **Files stay where they were written.** Selecting a store changes where NEW files go; it moves nothing. Delete a store only after the files it holds are gone.

## Related

- [model-providers](model-providers.md): the other provider rows an admin registers.
- [workspaces](workspaces.md): where an agent's files live while it works, as opposed to the artifacts kept for messages and forms.
