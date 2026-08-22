# Services

## 1. Purpose

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

## 2. Conceptual model

A service is a name and a pointer. The name is the public URL; the
pointer is the version currently active. Publishing appends an immutable
version and moves the pointer, so rollback is the same operation aimed
backwards, and nothing that has ever been served can change underneath a
viewer.

Serving is stateless by construction. A request resolves a name to a
version, streams content-addressed blobs, and (for a function call) runs
one python file in a fresh sandboxed process. No workspace is in the
path, which is why serving scales with API replicas rather than with the
authoring plane.

## 3. Architecture patterns implemented

- **Immutable versions with a movable pointer.** Publishing never mutates
  a served version; activation is a pointer swap and rollback is the same
  swap in reverse.
- **Exposure lives on the row, not in the bundle.** A publish must never
  silently flip a service public, so `viewer_auth` is service state that
  an operator sets, not manifest content an agent writes.
- **Content addressing all the way down.** Every bundle file is an
  artifact keyed by content, so `ETag` is the artifact id and
  `Cache-Control: immutable` is honest rather than optimistic.
- **The serving plane can be split off.** `PRIMER_SERVE_ONLY=1` mounts
  `/svc` plus observability and runs no worker, so service traffic can be
  kept off the control plane entirely.

## 4. Code layout

`primer/model/service.py` holds the entities; `primer/service/bundle.py`
validates an uploaded bundle as pure bytes-in with no I/O;
`primer/service/publish.py` persists a version; `primer/service/serve.py`
resolves names; `primer/service/gateway.py` executes functions;
`primer/api/routers/svc_serve.py` is the unprefixed serving router. The
console surface is `ui/components/services.jsx`.

## 5. Data model

### Entities (`primer/model/service.py`)

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

## 6. Lifecycle

### Publish pipeline

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

## 7. Persistence

Versions, their manifests and their function specs are storage rows;
bundle files are `Artifact` rows keyed by content. Retention keeps the
newest 20 versions plus the active one, whatever its age, so a rollback
target is never pruned out from under an operator. Nothing service-side
is cached durably: the name resolver's cache is a 5s per-process TTL and
the blob LRU is in memory, both rebuilt from storage on restart.

## 8. Public surfaces

### Serving plane (`primer/api/routers/svc_serve.py`)

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

### Functions gateway (`primer/service/gateway.py`)

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

### Console

The shell reaches services through the `services` overlay (list,
create/edit/delete with the standard modal) and `services::{id}` (Config
plus Versions tabs; activate/rollback; open-app link). Publishing is
deliberately not a console affordance: agents use the phase-4
`publish_service` tool.

## 9. Internal contracts

- **A published version is frozen.** `ServiceFunctionSpec[]` is derived at
  publish precisely so serving never re-parses source; a version's
  behaviour cannot drift from what was validated.
- **A yielding function fails the publish.** The gateway is synchronous,
  so an `@resumes` companion has nowhere to resume to. Rejecting at
  publish is the only point where the author is still present.
- **Docstring anatomy is relaxed, annotations are not.** Annotations build
  the request schema and are mandatory; the descriptions never reach LLM
  context, so requiring them would be ceremony.
- **Renaming a published service is a 409.** The name IS the public URL.
- **`_client` is reserved.** `GET /svc/_client/primer.js` serves the
  no-build client, so no service may claim that name.

## 10. Testing patterns

`tests/model/test_service.py`, `tests/service/test_bundle.py`,
`tests/toolset/test_python_registration_relaxed.py`,
`tests/api/test_services_crud.py`, `tests/api/test_services_publish.py`,
`tests/api/test_svc_serve.py`, `tests/api/test_svc_gateway.py`,
`tests/ui/test_services_page.py`, and the live-server lifecycle journey
`tests/e2e/test_services_publish_serve.py`.

## 11. Historical decisions

- **Serving is mounted without the `/v1` prefix.** Why: a service is a
  public web app at a URL a person types, not an API resource; putting it
  under the versioned API surface would have tied a user-facing URL to
  the API's own versioning.
- **The name cache is a short TTL rather than an invalidation protocol.**
  Why: publish and activate invalidate in-process, and other replicas
  converge within 5 seconds. A cross-replica invalidation protocol would
  add a coordination dependency to the one plane built to avoid them.
- **Publishing is a tool, not a console button.** Why: services are
  authored by agents in a workspace. An operator publishing by hand would
  be publishing a bundle they did not build.
