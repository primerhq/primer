---
slug: harnesses
title: Harnesses — git-installed entity bundles
summary: How primer manages git-backed bundles of agents, graphs, collections, documents, and toolsets via the fetch/install/sync/uninstall lifecycle.
related: [agents, graphs, knowledge, semantic-search]
mcp_tools:
  - harness::list
  - harness::get
  - harness::register
  - harness::update
  - harness::update_overrides
  - harness::fetch
  - harness::install
  - harness::sync
  - harness::uninstall
---

# Harnesses — git-installed entity bundles

## Overview

A **harness** is a primer-managed unit of "stuff that ships together
and gets installed as a group." Concretely, it's a git repository
containing a manifest plus Jinja2-templated entity definitions —
agents, graphs, collections, documents, toolsets — that primer
clones, renders against the operator's overrides, and applies as
storage rows. The result is a versioned bundle that can be updated
in one shot (re-pull + re-apply) or rolled back (uninstall + reinstall
at a prior ref).

The use case is sharing. A platform team writes a "support-bot"
harness that bundles two agents, one graph that orchestrates them,
and a knowledge collection seeded with the team's runbooks. They
push it to GitHub. Operators in different deployments install it
with one `harness::install` call. Each deployment gets its own
copies of the entity rows (with `harness_id` set so they're
recognised as managed), and the harness exposes per-deployment
overrides for the things that legitimately vary (e.g. the LLM
provider id, the channel id, an API base URL).

Harnesses are a worker-claimed entity, like sessions and chats —
the fetch, install, sync, and uninstall operations are run by the
worker pool, not synchronously inside the request. The MCP tools
are async: they return 202 + a status field; the agent polls
`harness::get` to see when the operation completes.

## Mental model

A `Harness` row:
- `id`, `slug` (operator-facing identifier; used as prefix in rendered
  entity ids — `{slug}__<template_name>`).
- `git_url`, `git_token` (optional, for private repos), `ref` (branch /
  tag / sha).
- `overrides` — operator-supplied JSON. Passed into Jinja2 as the
  variable namespace, shadowing the harness's own defaults.
- `status` — `draft | ready | installed | outdated | error`. The
  lifecycle state machine. See below.
- `bundle_hash` — sha256 of the cloned manifest + rendered entities.
  Used by sync's fast-path.
- `overrides_hash` — sha256 of the overrides JSON.
- `error` — single-line error message when `status=error`.

The render pipeline:
1. Clone `git_url@ref` (with `git_token` if present) to a temp dir.
2. Read the manifest at `harness.yaml` (or similar — file shape
   defined by the harness spec).
3. For each template entity, render with the overrides + harness
   metadata as the Jinja2 context.
4. Cross-references between entities are rewritten so the rendered
   ids are `{slug}__<template_name>`. So an agent template that
   references `{{ tools.search }}` becomes a real id like
   `support-bot__search` in storage.
5. Document `content_path` fields are read from the checkout — the
   loader resolves them relative to the manifest root.
6. The set of rendered entities is the "bundle." Install applies it
   to storage; sync diffs it against the existing bundle.

Managed entity rows are tagged with `harness_id=<harness_id>`. The
generic CRUD endpoints refuse PUT and DELETE on rows with that field
set (409 with reason "managed by harness <id>"). The harness's own
sync / uninstall handle mutation; the rest of the system stays out.

## Lifecycle and states

`Harness.status` transitions:

- **draft** — newly registered. `git_url` and `ref` are set; nothing
  has been fetched. Next op: `fetch`.
- **ready** — fetched and validated. The clone exists, the manifest
  parsed, the templates rendered to entities (in memory). Nothing in
  storage yet. Next op: `install`.
- **installed** — entities applied to storage. The harness is live;
  managed entities are visible everywhere. Next op: `sync` (when
  upstream changes) or `uninstall`.
- **outdated** — installed but the upstream ref has moved. Operator
  hint to run `sync`. Detected by a periodic remote-tracking poll.
- **error** — a fetch/install/sync/uninstall failed. `error` field
  carries the message. The state of storage is the state at the
  moment the op failed — partial installs are atomic per entity but
  not across entities.

The four operations:

- **fetch** — claims the harness, clones, validates, transitions to
  `ready` (or `error`).
- **install** — claims, applies rendered entities (create-or-update,
  by id), transitions to `installed`. Re-installing replaces.
- **sync** — claims, re-fetches if upstream `ref` changed, computes
  the new `bundle_hash`. If the new hash matches the stored hash AND
  `overrides_hash` matches, sync is a no-op (fast-path). Otherwise
  diffs and applies. Transitions stay `installed`.
- **uninstall** — claims, deletes every managed entity owned by the
  harness, transitions to `draft`. The harness row stays; re-install
  is one click.

All four are claim-based (via the harness `ClaimAdapter`). They can
be queued — `harness::install` followed by `harness::sync` will
process in order, not interleaved.

## MCP tools

The `harness` reserved toolset exposes a tighter, harness-aware
surface than the generic system CRUD. Use `harness::*` for the
verb-style operations and `system::list_harnesses` etc. for plain
CRUD.

### Discovery and inspection

- `harness::list` — paginated listing. Same shape as `system::list_harnesses`.
- `harness::get` — fetch by id. Includes computed fields like
  `last_synced_at`, `bundle_hash`.

### Registration

- `harness::register` — create a draft row. Body: `id`, `slug`,
  `git_url`, `ref`, optional `git_token`, optional `overrides`.
  Does NOT fetch — explicit `harness::fetch` next.
- `harness::update` — partial update. Editing `git_url` / `ref`
  while installed marks the row outdated; sync to apply.
- `harness::update_overrides` — partial update of the overrides
  dict only. Editing overrides while installed marks outdated;
  sync to apply.

### Lifecycle operations

- `harness::fetch` — clone + validate. Returns 202; poll `get` to
  see `status=ready` (or `error`).
- `harness::install` — apply to storage. Returns 202. Idempotent —
  re-installing replaces the bundle.
- `harness::sync` — diff + update. Fast-path no-op if no changes
  upstream. Returns 202.
- `harness::uninstall` — remove managed entities. Returns 202.
  The Harness row stays (re-install is one click); only the
  managed entities are deleted.

## Workflows

### Workflow 1 — install a community harness

**Goal.** Operator wants the open-source `code-review` harness
installed at the latest 1.x tag.

1. Register:

```json
{
  "tool": "harness::register",
  "arguments": {
    "id": "h-code-review",
    "slug": "code-review",
    "git_url": "https://github.com/example/primer-code-review.git",
    "ref": "v1.4.0",
    "overrides": {
      "llm_provider_id": "lp-claude",
      "max_concurrent_reviews": 3
    }
  }
}
```

Status is `draft`.

2. Fetch:

```json
{
  "tool": "harness::fetch",
  "arguments": {"id": "h-code-review"}
}
```

Returns 202. Poll:

```json
{
  "tool": "harness::get",
  "arguments": {"id": "h-code-review"}
}
```

Until `status` is `ready` (a few seconds for small repos, longer
for repos with large checkouts).

3. Install:

```json
{
  "tool": "harness::install",
  "arguments": {"id": "h-code-review"}
}
```

Poll until `status=installed`. The agents `code-review__lint`,
`code-review__verdict`, etc. now exist as Agent rows with
`harness_id=h-code-review`.

### Workflow 2 — update to a newer version

**Goal.** Upstream tagged v1.5.0. Pull the changes.

1. Update the ref:

```json
{
  "tool": "harness::update",
  "arguments": {"id": "h-code-review", "ref": "v1.5.0"}
}
```

`status` becomes `outdated`.

2. Sync:

```json
{
  "tool": "harness::sync",
  "arguments": {"id": "h-code-review"}
}
```

Returns 202. Worker re-fetches, recomputes the bundle, applies
diffs (creates new entities the upstream added; updates changed
ones; deletes removed ones — all only within the harness's managed
set). Status returns to `installed`. If something went wrong,
`status=error` and `error` carries the reason.

## Gotchas

- **Managed entity CRUD is blocked.** Trying to PUT or DELETE a
  managed Agent / Graph / Collection / Document via the regular
  CRUD endpoints returns 409 with reason "managed by harness <id>".
  Updates must go through `harness::sync` after the upstream
  changes (and optionally `harness::update_overrides` to change
  what gets rendered).
- **Override changes mark the harness outdated, but don't auto-
  sync.** The operator decides when to actually re-apply. This is
  intentional — changing overrides at install time is rare; staging
  the change in the row first lets you preview the diff.
- **Cross-entity references are rewritten by slug.** Inside the
  bundle, a graph node references `agent: {{ agents.lint }}` and
  the renderer turns it into `agent_id: "code-review__lint"`. If
  two harnesses both define a `lint` agent, the slug prefix keeps
  them distinct in storage.
- **Document `content_path` is read at render time, not at
  request time.** The content lives in the harness git repo; the
  rendered Document.meta['content'] is the file bytes at the time
  install/sync ran. Editing the file in the local checkout doesn't
  affect the installed Document.
- **Delete is async (202), not sync.** The uninstall path is
  worker-claimed. After the 202, the rows still exist briefly.
  Poll `harness::get` to confirm the harness is back to `draft`.
- **Jinja2 sandbox.** Templates can't import modules, call
  `__getattr__` on builtins, or escape into Python. Constants,
  filters, and the override variables are the only context. This
  is for security — harnesses are remote code.
- **Fast-path sync requires the manifest's `bundle_hash` to be
  deterministic.** If the upstream's render isn't deterministic
  (e.g. it includes a timestamp), every sync will re-apply, which
  is wasted work. Treat non-deterministic templates as bugs.
- **`git_token` is write-only.** GET/list responses mask it. Re-
  fetching a Harness row and re-POSTing it would zero the token;
  use partial updates.
- **Reinstalling at a different ref skips the explicit uninstall
  step.** The new install diff treats removed entities as
  delete-required, so the workflow `update(ref=...) → sync` is
  enough; no `uninstall` needed.

## Related

- [agents](agents.md) — harnesses commonly ship multiple agents.
- [graphs](graphs.md) — harnesses commonly bundle a graph that
  orchestrates the agents.
- [knowledge](knowledge.md) — harness Documents seed user
  collections.
- [semantic-search](semantic-search.md) — installed agents/graphs
  are automatically indexed by the IC subsystem; `search::search_agents`
  picks them up.
