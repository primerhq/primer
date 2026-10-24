---
title: "What a harness is"
summary: "A harness is a git repository of agents, graphs and documents that primer installs and keeps in sync. List and inspect them."
order: 1
tools: [harness__harness__list, harness__harness__get]
related: [harnesses/install-and-sync, agents/create-and-edit]
---
## What it is
A harness is a git repository of agents, graphs, collections, documents and toolsets that primer installs as a group and keeps in sync. Primer clones the repository, renders its templates against the harness's overrides, and writes each entity as a storage row tagged with the harness. The two reads here are open to every user; every write needs an admin.

## Concepts
- **The bundle.** A harness is a git repository with a `harness.yaml` manifest, an `overrides.schema.json`, and Jinja2-templated definitions of agents, graphs, collections, documents and toolsets. Installing clones the repository at the row's `ref` (an optional `subpath` narrows the repository), renders every template, and applies the results as rows.
- **Overrides.** `overrides` is operator-supplied JSON, validated against the cached `overrides.schema.json`, and passed into the templates. The renderer is sandboxed and exposes only `overrides` and the harness context (`slug`, `name`, `description`), so a template cannot reach the host.
- **Identity.** Every entity a harness installs gets the id `<slug>__<template name>`, so two harnesses can ship a `lint` agent without a collision. A slug is kebab-case and may not contain `__`, which is reserved as the id separator; your own entity ids avoid it for the same reason.
- **Managed rows.** An installed entity carries a `harness_id` backpointer, and that lock makes it read-only to the normal tools: the update and delete tools for agents, graphs, collections, model profiles and toolsets refuse a managed row with `type=conflict`, and a REST PUT or DELETE answers 409 `managed_entity`. The lock is how a harness stays in step with its bundle. To change what renders, change the harness's overrides and sync.
- **Direction.** A harness is `inbound` (the default): it pulls a remote bundle into local storage, and its uninstall deletes the installed objects, the rendering snapshot and the harness row itself. An `outbound` harness does the reverse: it points at live local entities, templatizes chosen fields into overrides, and pushes a renderable bundle back to the git remote. An outbound harness never stamps `harness_id`, so its tracked entities stay user-owned and mutable: drift is detected there, not locked.
- **Subharnesses.** A parent `harness.yaml` may declare dependencies on other repositories, walked transitively. A sub renders under its own slug, but every entity it lands is stamped with the parent's `harness_id`, so one uninstall removes the whole subtree.
- **Status.** The row's `status` is one of `draft` (newly registered), `ready` (fetched and validated), `installed` (entities applied), `outdated` (upstream moved, or the overrides changed) and `error` (an operation failed; `last_operation_error` carries the reason). The operations are asynchronous: `fetch`, `install`, `sync` and `uninstall` are queued for a worker, and while one is pending the row holds `pending_operation` and refuses updates. Poll `harness__harness__get` until it clears.
- **The git coordinates.** `git_url` must be `https://` with a host, and `ref` is a branch, tag or commit. `git_token` is write-only for private repositories: reads mask it as `**********`, and they mask a credential kept in the URL the same way.
- **The writes.** Registering, configuring, fetching, installing, syncing and uninstalling live on [install and sync a harness](harnesses/install-and-sync), and every one of them needs an admin.

## How to
1. List what is registered with `harness__harness__list`, filtering by `slug` or `status`.
2. Read a row with `harness__harness__get`: it carries the git coordinates, the status, the overrides and any pending operation.
3. While an operation is in flight, keep polling the row until `pending_operation` is clear and the status settles.
4. To change anything about a harness, use the write page: every write needs an admin.

## Tool calls
### List the harnesses
`harness__harness__list` answers a page with `items`, `total`, `offset` and `length`, optionally filtered by `slug` or `status`.

```json
{"toolset_id": "harness", "tool_name": "harness__list", "arguments": {"status": "installed", "length": 20}}
```

Reference: [`harness__harness__list`](reference/tools/harness/harness__list)

### Read a harness row
`harness__harness__get` returns the full row by `id`, or `type=not-found` when the id is unknown.

```json
{"toolset_id": "harness", "tool_name": "harness__get", "arguments": {"id": "hns_a1b2c3d4e5f6"}}
```

Reference: [`harness__harness__get`](reference/tools/harness/harness__get)

## Gotchas
- The reads are open to every user, but every write and operation needs an admin; a lower role is refused with `forbidden`.
- An enqueue is not the operation: `fetch`, `install`, `sync` and `uninstall` answer at once and a worker runs them; poll the row for the result.
- While an operation is pending, the harness cannot be updated or enqueued again: the call is refused with `type=conflict`.
- A managed row is locked to its harness: the agent and graph update and delete tools refuse it with `type=conflict`, and REST PUT or DELETE answers 409. Change the bundle and sync instead.
- `git_token` and a URL credential are masked on every read: you cannot read a secret back, and registering a harness from a masked URL is refused.
- An `outbound` harness refuses `fetch`, `install` and `sync` with `type=direction-mismatch`, and its uninstall keeps the objects it tracks.
- The ids it installs carry the slug, `ops-suite__lint` rather than `lint`: two harnesses cannot ship the same slug.

## Related
[install and sync a harness](harnesses/install-and-sync), [create and edit an agent](agents/create-and-edit).
