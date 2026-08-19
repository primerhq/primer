# Services

Agent-published web apps, versioned as immutable snapshots and served at
`/svc/{name}/` by any API replica. A service's dynamic behaviour is
stateless by construction: bundle python functions execute per-request
in the python-runner sandbox, so serving scales with API replicas and no
workspace is ever in the serving path. Workspaces are where agents
author bundles; publishing detaches the app from them entirely.

Design rationale and the full decision trail live in the (local, not
committed) spec `docs/superpowers/specs/2026-08-08-services-design.md`.
This page documents what is implemented: spec phases 1-2. Phase 3 (tool
dispatch through a manifest allowlist, `viewer_auth="none"` end-to-end
with an anonymous service principal) and phase 4 (system tools
parity, agent-usage docs) are not built yet; where they change behaviour
below it is called out.

## Entities (`primer/model/service.py`)

- `Service` (Describeable): `name` (URL slug, `[a-z0-9][a-z0-9-]{1,62}`,
  `_client` reserved), `active_version_id` (None = unpublished),
  `viewer_auth` (`console` | `none`), `harness_id`. The exposure setting
  lives on the row, NOT in the manifest: a publish must never silently
  flip a service public. Renaming a published service is a 409 because
  the name is the public URL.
- `ServiceVersion` (Identifiable): `service_id`, monotonic `version`,
  the parsed `ServiceManifest`, `files` (bundle path -> `Artifact` id),
  and `ServiceFunctionSpec[]` derived at publish so serving never
  re-parses source. Immutable once created.
- `ServiceManifest` (`service.yaml`, `extra="forbid"`): `entry`
  (default `index.html`), `functions` (python files; auto-populated
  with `functions.py` when present), `tools` (the gateway allowlist;
  validated to existing toolsets at publish, ENFORCED in phase 3).

## Publish pipeline

`POST /v1/services/{id}/versions` (gzipped tar body) ->
`primer/service/bundle.py::validate_bundle` (pure bytes-in, no I/O):

- caps: 10 MiB uncompressed, 200 files, regular files only, no
  absolute/escaping paths;
- manifest parse with unknown-key rejection;
- every functions file runs through the python-runner registration
  (`register_module(..., require_docstrings=False, allow_yielding=False)`):
  annotations are still mandatory (they build the schema), the
  docstring anatomy is not (these descriptions never reach LLM
  context), and any `@resumes` companion fails the publish because the
  gateway is synchronous.

`primer/service/publish.py` then persists: one `Artifact` row per file,
the version row, the active pointer (unless `?activate=false`), and
retention pruning (newest 20 kept, the active version always kept).
`POST /v1/services/{id}/_activate` repoints (rollback = pointing back).
Deleting a service cascades versions and artifacts.

## Serving plane (`primer/api/routers/svc_serve.py`)

Mounted WITHOUT the `/v1` prefix. `GET /svc/{name}/{path}`:

- name resolution through a 5s-TTL per-process cache
  (`primer/service/serve.py::ServiceResolver`); publish/activate
  invalidate in-process, other replicas converge via the TTL;
- `viewer_auth=console` applies `require_user` per request (the auth
  middleware only populates identity); `none` serves anonymously;
- path pick: exact file, else extension-less misses fall back to the
  manifest entry (SPA client routing), misses with an extension 404;
- blobs stream through a 64 MiB LRU with `ETag` = artifact id and
  `Cache-Control: immutable` (bundle files are content-addressed);
  If-None-Match returns 304;
- 404s are a friendly HTML page for browsers and
  `application/problem+json` otherwise.

`PRIMER_SERVE_ONLY=1` turns a process into a dedicated serving replica:
only `/svc` plus the always-on observability surface is mounted and no
worker runs. This is the scale-out escape hatch for keeping service
traffic off the control plane.

## Functions gateway (`primer/service/gateway.py`)

`POST /svc/{name}/_gateway/functions/{fn}` validates the JSON body
against the published schema, loads the source file from artifacts, and
executes in the SAME hardened runner the python toolsets use (fresh
process, rlimits, timeout from the `@primer_tool` decorator). The
context that crosses the boundary is service data only; there is no
session, workspace, or `inform`. Failures map to: 404 unknown
function, 422 schema mismatch, 503 runner unavailable, 500 with the
exception type/message (traceback included only under
`viewer_auth=console`). `GET /svc/_client/primer.js` serves the no-build
client (`Primer.fn(name, args)`; `Primer.tool` arrives with phase 3).

## Console

`/services` (list, create/edit/delete with the standard modal) and
`/services/{id}` (Config + Versions tabs; activate/rollback; open-app
link). Publishing is deliberately not a console affordance: agents use
the phase-4 `publish_service` tool.

## Tests

`tests/model/test_service.py`, `tests/service/test_bundle.py`,
`tests/toolset/test_python_registration_relaxed.py`,
`tests/api/test_services_crud.py`, `tests/api/test_services_publish.py`,
`tests/api/test_svc_serve.py`, `tests/api/test_svc_gateway.py`,
`tests/ui/test_services_page.py`, and the live-server lifecycle journey
`tests/e2e/test_services_publish_serve.py`.
