---
title: "Install and sync a harness"
summary: "Admin: register, configure, fetch, install, sync and uninstall a harness."
audience: admin
order: 2
tools: [harness__harness__register, harness__harness__update, harness__harness__update_overrides, harness__harness__fetch, harness__harness__install, harness__harness__sync, harness__harness__uninstall]
related: [harnesses/what-a-harness-is]
---
## What it is
The seven writes of the harness lifecycle: register a git repository as a harness, configure it, fetch it, install it, sync it, and uninstall it. Every tool on this page needs an admin. Four of them (`harness__harness__fetch`, `harness__harness__install`, `harness__harness__sync`, `harness__harness__uninstall`) queue a worker operation and answer at once; the other three write the row directly.

## Concepts
- **Register.** `harness__harness__register` creates a `draft` row from `name`, `slug` and `git_url` (`https://` with a host), plus optional `ref` (a branch, tag or commit; defaults to `main`), `subpath`, `git_token` and `description`. The `id` is server-assigned with the `hns_` prefix; the slug is unique and may not contain `__`. Registration does not fetch.
- **Configure.** `harness__harness__update` changes `name`, `description`, `ref`, `subpath` and `git_token`; it cannot move `git_url`. Changing `ref` or `subpath` marks the overrides dirty. A `git_token` of `**********` (the mask a read serves) keeps the stored token, and `""` clears it.
- **Overrides.** `harness__harness__update_overrides` validates the whole `overrides` dictionary against the cached `overrides.schema.json` and stores it. Before a fetch has cached a schema it answers `type=overrides-schema-missing`, and a dictionary that does not validate answers `type=overrides-invalid`.
- **The worker operations.** `fetch`, `install`, `sync` and `uninstall` each set `pending_operation` on the row and announce it to the claim engine; the call answers at once and a worker does the work. Poll `harness__harness__get` until `pending_operation` is clear and the status settles. A second enqueue while one is pending is refused with `type=conflict`.
- **Fetch.** The worker clones the repository at `ref` with the stored token, reads `harness.yaml` and `overrides.schema.json`, walks the declared dependencies, and caches the bundle and the schema. A fresh harness lands on `ready`; when upstream has moved or the overrides are dirty it lands on `outdated`, and a fetch of an installed harness that has not moved keeps `installed`.
- **Install.** Applies a fetched bundle: renders every template with the stored overrides, validates every entity, and writes them in dependency order, each with the id `<slug>__<template name>` and the `harness_id` tag. It requires the status to be `draft`, `ready` or `outdated` and a cached schema. Before it writes, the shared toolset rules run over every toolset the bundle would create or change: a bundle that would create or change a stdio MCP or Python toolset is refused as a whole, with `toolset_needs_admin` recorded in `last_operation_error`, unless the requester (the run's initiator, or the caller over `/v1/mcp`) is an admin; a call with no known requester fails closed.
- **Sync.** Reconciles the installed rows with the fetched bundle within the harness's managed set: it deletes what upstream removed, creates what it added, and updates what changed. It requires the status to be `installed` or `outdated` and a fetched bundle (`type=fetch-required` otherwise), and it is a no-op when the bundle and the overrides have not moved.
- **Uninstall.** Removes the harness. An inbound one removes the installed objects in reverse order, the rendering snapshot and the row itself; an outbound one keeps the objects it merely tracks. Afterward the harness is gone from `harness__harness__list`.
- **Outbound harnesses.** `fetch`, `install` and `sync` are inbound operations: on an outbound harness they are refused with `type=direction-mismatch`.
- **The default gates.** `install`, `sync` and `uninstall` are approval-gated by default, and their approvers are an admin only: a run an admin started parks on the approval card, and the install and sync cards show the overrides the operation applies. An approval binds the inputs it was shown: the operation applies the inputs it was asked for, or nothing. If an input changes before it runs, the worker refuses it before anything is cloned or written (the status is unchanged, and `last_operation_error` records `operation_inputs_changed`), so a new decision is needed. Over `/v1/mcp`, a gated call is refused as `approval_required`. The bare ids carry `__`, so `system__call_tool` is their one door in a run, and it meets the gate. A person acting over the REST API is not gated.

## How to
1. Register the repository with `harness__harness__register`; the row answers with its server-assigned `id` and the status `draft`.
2. Fetch it with `harness__harness__fetch` and poll `harness__harness__get` until the status is `ready` (or `outdated`).
3. Set the overrides with `harness__harness__update_overrides`, validated against the schema the fetch cached.
4. Install with `harness__harness__install`, naming the stored overrides unchanged. The call parks for approval (the default gate), and the card shows those overrides. Poll until the status is `installed`.
5. When upstream moves: change `ref` with `harness__harness__update`, fetch, then `harness__harness__sync` with the stored overrides.
6. Remove the harness with `harness__harness__uninstall`; an inbound one takes the installed objects and the row with it.

## Tool calls
### Register a harness
`harness__harness__register` creates the `draft` row and answers with it. It does not fetch.

```json
{"toolset_id": "harness", "tool_name": "harness__register", "arguments": {"name": "Code review", "slug": "code-review", "git_url": "https://github.com/example/primer-code-review.git", "ref": "v1.4.0"}}
```

Reference: [`harness__harness__register`](reference/tools/harness/harness__register)

### Change the ref
`harness__harness__update` changes the mutable metadata; a new `ref` marks the overrides dirty.

```json
{"toolset_id": "harness", "tool_name": "harness__update", "arguments": {"id": "hns_a1b2c3d4e5f6", "ref": "v1.5.0"}}
```

Reference: [`harness__harness__update`](reference/tools/harness/harness__update)

### Set the overrides
`harness__harness__update_overrides` stores the dictionary after validating it against the cached schema.

```json
{"toolset_id": "harness", "tool_name": "harness__update_overrides", "arguments": {"id": "hns_a1b2c3d4e5f6", "overrides": {"llm_provider_id": "lp-claude", "max_concurrent_reviews": 3}}}
```

Reference: [`harness__harness__update_overrides`](reference/tools/harness/harness__update_overrides)

### Fetch the bundle
`harness__harness__fetch` enqueues the fetch; poll `harness__harness__get` until `pending_operation` clears.

```json
{"toolset_id": "harness", "tool_name": "harness__fetch", "arguments": {"id": "hns_a1b2c3d4e5f6"}}
```

Reference: [`harness__harness__fetch`](reference/tools/harness/harness__fetch)

### Install the harness
`harness__harness__install` enqueues the install, naming the stored overrides unchanged; a different value is refused with `type=conflict`.

```json
{"toolset_id": "harness", "tool_name": "harness__install", "arguments": {"id": "hns_a1b2c3d4e5f6", "overrides": {"llm_provider_id": "lp-claude", "max_concurrent_reviews": 3}}}
```

Reference: [`harness__harness__install`](reference/tools/harness/harness__install)

### Sync the harness
`harness__harness__sync` reconciles the installed rows with the fetched bundle, again naming the stored overrides.

```json
{"toolset_id": "harness", "tool_name": "harness__sync", "arguments": {"id": "hns_a1b2c3d4e5f6", "overrides": {"llm_provider_id": "lp-claude", "max_concurrent_reviews": 3}}}
```

Reference: [`harness__harness__sync`](reference/tools/harness/harness__sync)

### Uninstall the harness
`harness__harness__uninstall` enqueues the removal; an inbound harness takes its installed objects and its row with it.

```json
{"toolset_id": "harness", "tool_name": "harness__uninstall", "arguments": {"id": "hns_a1b2c3d4e5f6"}}
```

Reference: [`harness__harness__uninstall`](reference/tools/harness/harness__uninstall)

## Gotchas
- Every tool on this page needs an admin; a lower role is refused with `forbidden`.
- An enqueue is not the operation: the four worker calls answer at once, and a Stop waits for them rather than cancelling them. Poll the row for the result.
- While an operation is pending, `harness__harness__update` and `harness__harness__update_overrides` answer `type=conflict`, and a second enqueue is refused the same way.
- `install` and `sync` refuse anything but the stored overrides: the value the approval card shows is the value the worker renders.
- An enqueued operation that loses an input to an edit is refused before it runs, with `operation_inputs_changed` (or `operation_inputs_unrecorded`) in `last_operation_error`; the status is unchanged, and the operation must be requested again.
- An install or sync that would create or change a stdio MCP or Python toolset fails with `toolset_needs_admin` unless the requester is an admin; the whole bundle is refused and nothing is written.
- A `git_token` of `**********` keeps the stored token and `""` clears it; `harness__harness__update` cannot move `git_url` at all.
- Registering a harness from the masked URL a read served is refused: there is no stored URL to take the credential from.
- An `outbound` harness refuses `fetch`, `install` and `sync` with `type=direction-mismatch`.
- A status of `error` carries a structured code in `last_operation_error` (`harness_yaml_missing`, `fetch_required`, `toolset_needs_admin`, `apply_id_conflict`, among others); fix what it names and re-run the operation.
- Two harnesses cannot share a slug, and the ids they install are its namespace: the `lint` agent of `code-review` is `code-review__lint`.

## Related
[what a harness is](harnesses/what-a-harness-is).
