---
slug: api-harnesses
title: Harnesses API
section: reference
summary: REST endpoints to register, fetch, install, sync, build, push, update tracked entities, and uninstall harnesses.
---

A harness is a git-backed, Jinja2-templated bundle of Primer entities (agents, graphs, collections, toolsets) that can be installed from a remote repository (inbound) or assembled from live entities and pushed to one (outbound).

```ref:concepts/what-is-an-agent
Agents and the entities a harness manages.
```

```ref:features/harnesses
Create and manage harnesses in the console.
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/harnesses` | List harnesses |
| POST | `/v1/harnesses` | Create a harness (DRAFT) |
| GET | `/v1/harnesses/{harness_id}` | Get a harness by id |
| PUT | `/v1/harnesses/{harness_id}` | Update a harness |
| DELETE | `/v1/harnesses/{harness_id}` | Enqueue UNINSTALL |
| POST | `/v1/harnesses/{harness_id}/fetch` | Fetch bundle from remote |
| POST | `/v1/harnesses/{harness_id}/install` | Install (render templates into entities) |
| POST | `/v1/harnesses/{harness_id}/sync` | Sync to latest ref |
| POST | `/v1/harnesses/{harness_id}/build` | Build outbound bundle from tracked entities |
| POST | `/v1/harnesses/{harness_id}/push` | Push outbound bundle to git |
| PUT | `/v1/harnesses/{harness_id}/overrides` | Set override values |
| PUT | `/v1/harnesses/{harness_id}/tracked_entities` | Replace tracked entity set |

## Harness object

```json
{
  "id": "hrn-a1b2c3d4e5f6",
  "slug": "my-harness",
  "name": "My Harness",
  "description": "Coding assistants bundle.",
  "git_url": "https://github.com/example/harness-bundle",
  "ref": "main",
  "subpath": null,
  "direction": "inbound",
  "status": "installed",
  "pending_operation": null,
  "bundle_hash": "sha256:abc123",
  "available_bundle_hash": "sha256:abc123",
  "available_commit": "deadbeef",
  "resolved_commit": "deadbeef",
  "commits_ahead": false,
  "overrides": {"provider_id": "anthropic-prod", "model_name": "claude-sonnet-4-6"},
  "overrides_schema": {"type": "object", "properties": {}},
  "overrides_hash": "sha256:def456",
  "overrides_dirty": false,
  "schema_hash": "sha256:ghi789",
  "schema_missing_input": false,
  "tracked_entities": [],
  "dependencies_resolved": [],
  "last_operation_at": "2026-06-07T19:00:55.000000Z",
  "last_operation_error": null,
  "last_pushed_at": null,
  "last_pushed_commit": null,
  "last_pushed_bundle_hash": null,
  "created_at": "2026-06-07T18:00:00.000000Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | System-assigned identifier |
| `slug` | string | URL-safe identifier (2-64 chars) |
| `name` | string | Human-readable name |
| `git_url` | string | Remote git URL for the bundle |
| `ref` | string | Git branch or tag (default: `main`) |
| `subpath` | string or null | Optional subdirectory within the repo |
| `direction` | `inbound` or `outbound` | Whether this harness installs or publishes |
| `status` | string | One of `draft`, `ready`, `installed`, `outdated`, `error` |
| `pending_operation` | string or null | Active async operation: `fetch`, `install`, `sync`, `uninstall`, `build`, `push` |
| `commits_ahead` | boolean | True when the remote has commits not yet synced |
| `overrides` | object | Current override values |
| `overrides_schema` | object or null | JSON Schema describing available override fields |
| `overrides_dirty` | boolean | True when overrides changed since last install |
| `tracked_entities` | array | Outbound tracked entity list |
| `dependencies_resolved` | array | Transitive dependency tree resolved at last fetch |

## Create a harness

`POST /v1/harnesses` - returns `201 Created` with the harness object.

Required fields: `name`, `slug`, `git_url`. The harness starts in `draft` status; call `/fetch` to retrieve the bundle.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/harnesses \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Harness",
    "slug": "my-harness",
    "git_url": "https://github.com/example/harness-bundle",
    "ref": "main",
    "direction": "inbound"
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/harnesses",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "name": "My Harness",
        "slug": "my-harness",
        "git_url": "https://github.com/example/harness-bundle",
        "ref": "main",
        "direction": "inbound",
    },
)
assert r.status_code == 201
harness_id = r.json()["id"]
--- javascript
const r = await fetch("/v1/harnesses", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    name: "My Harness",
    slug: "my-harness",
    git_url: "https://github.com/example/harness-bundle",
    ref: "main",
    direction: "inbound"
  })
})
const harness = await r.json()
```

**Errors:** `409` if a harness with the same `slug` already exists.

## Get a harness

`GET /v1/harnesses/{harness_id}` - returns `200 OK` with the harness object.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6 \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6",
    headers={"Authorization": f"Bearer {token}"},
)
h = r.json()
--- javascript
const r = await fetch("/v1/harnesses/hrn-a1b2c3d4e5f6", {
  headers: {"Authorization": `Bearer ${token}`}
})
const h = await r.json()
```

**Errors:** `404` if the id does not exist.

## List harnesses

`GET /v1/harnesses` - returns an offset or cursor page of harness objects.

Query parameters: `slug` (filter), `status` (filter), `direction` (filter), `limit` (1-200, default 20), `offset`, `cursor`.

```code-tabs:curl,python,javascript
--- curl
curl "https://your-host/v1/harnesses?status=installed&limit=20" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://your-host/v1/harnesses",
    headers={"Authorization": f"Bearer {token}"},
    params={"status": "installed", "limit": 20},
)
items = r.json()["items"]
--- javascript
const r = await fetch("/v1/harnesses?status=installed&limit=20", {
  headers: {"Authorization": `Bearer ${token}`}
})
const {items} = await r.json()
```

## Fetch bundle from remote

`POST /v1/harnesses/{harness_id}/fetch` - pulls the bundle from the remote git URL and caches the overrides schema. Returns `200` or `202` (async). Poll `GET /v1/harnesses/{harness_id}` until `pending_operation` is null.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/fetch \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx, time
r = httpx.post(
    "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/fetch",
    headers={"Authorization": f"Bearer {token}"},
)
assert r.status_code in (200, 202)
# Poll until idle
while True:
    h = httpx.get(
        "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    if not h.get("pending_operation"):
        break
    time.sleep(0.5)
--- javascript
await fetch("/v1/harnesses/hrn-a1b2c3d4e5f6/fetch", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`}
})
```

After fetch completes, `overrides_schema` is populated on the harness object.

## Set override values

`PUT /v1/harnesses/{harness_id}/overrides` - stores the override values used during install/sync. Returns `200` or `204`.

```code-tabs:curl,python,javascript
--- curl
curl -X PUT https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/overrides \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider_id": "anthropic-prod", "model_name": "claude-sonnet-4-6"}'
--- python
import httpx
r = httpx.put(
    "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/overrides",
    headers={"Authorization": f"Bearer {token}"},
    json={"provider_id": "anthropic-prod", "model_name": "claude-sonnet-4-6"},
)
assert r.status_code in (200, 204)
--- javascript
await fetch("/v1/harnesses/hrn-a1b2c3d4e5f6/overrides", {
  method: "PUT",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({provider_id: "anthropic-prod", model_name: "claude-sonnet-4-6"})
})
```

## Install

`POST /v1/harnesses/{harness_id}/install` - renders bundle templates into live entities. Returns `200` or `202`. Poll until `pending_operation` is null.

Entities created by install carry a `harness_id` field and are write-protected; direct `PUT`/`DELETE` on them returns `409`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/install \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx, time
r = httpx.post(
    "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/install",
    headers={"Authorization": f"Bearer {token}"},
)
assert r.status_code in (200, 202)
while True:
    h = httpx.get(
        "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    if not h.get("pending_operation"):
        break
    time.sleep(0.5)
assert h["status"] == "installed"
--- javascript
await fetch("/v1/harnesses/hrn-a1b2c3d4e5f6/install", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`}
})
```

**Errors:** `409` if the harness is already being operated on.

## Sync to latest ref

`POST /v1/harnesses/{harness_id}/sync` - re-fetches the bundle and re-renders entities in place. Returns `200` or `202`. Safe to call when status is `outdated`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/sync \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/sync",
    headers={"Authorization": f"Bearer {token}"},
)
assert r.status_code in (200, 202)
--- javascript
await fetch("/v1/harnesses/hrn-a1b2c3d4e5f6/sync", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Build outbound bundle

`POST /v1/harnesses/{harness_id}/build` - renders tracked live entities into bundle templates. Requires `direction: outbound` and a populated `tracked_entities` list. Returns `200` or `202`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/build \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/build",
    headers={"Authorization": f"Bearer {token}"},
)
assert r.status_code in (200, 202)
--- javascript
await fetch("/v1/harnesses/hrn-a1b2c3d4e5f6/build", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Push outbound bundle to git

`POST /v1/harnesses/{harness_id}/push` - commits and pushes the built bundle to the configured remote. Requires a `git_token` with push access. Returns `200` or `202`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/push \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/push",
    headers={"Authorization": f"Bearer {token}"},
)
assert r.status_code in (200, 202)
--- javascript
await fetch("/v1/harnesses/hrn-a1b2c3d4e5f6/push", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Update tracked entities

`PUT /v1/harnesses/{harness_id}/tracked_entities` - replaces the full tracked entity set for an outbound harness. Each entry maps a live entity to a template name.

```code-tabs:curl,python,javascript
--- curl
curl -X PUT https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/tracked_entities \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tracked_entities": [
      {"kind": "agent", "source_id": "my-agent", "template_name": "agent-main", "overrides": []}
    ]
  }'
--- python
import httpx
r = httpx.put(
    "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6/tracked_entities",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "tracked_entities": [
            {"kind": "agent", "source_id": "my-agent",
             "template_name": "agent-main", "overrides": []},
        ]
    },
)
--- javascript
await fetch("/v1/harnesses/hrn-a1b2c3d4e5f6/tracked_entities", {
  method: "PUT",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    tracked_entities: [
      {kind: "agent", source_id: "my-agent", template_name: "agent-main", overrides: []}
    ]
  })
})
```

**TrackedEntity fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `kind` | yes | One of `agent`, `graph`, `collection`, `document`, `toolset` |
| `source_id` | yes | Id of the live entity to track |
| `template_name` | yes | Template filename (without extension) in the bundle |
| `overrides` | no | Override mapping list |

## Uninstall a harness

`DELETE /v1/harnesses/{harness_id}` - enqueues an UNINSTALL operation that removes all harness-managed entities and lifts write-protection. Returns `200`. Poll `GET /v1/harnesses/{harness_id}` until the harness is gone or `status` transitions away from `installed`.

```code-tabs:curl,python,javascript
--- curl
curl -X DELETE https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6 \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete(
    "https://your-host/v1/harnesses/hrn-a1b2c3d4e5f6",
    headers={"Authorization": f"Bearer {token}"},
)
assert r.status_code == 200
--- javascript
await fetch("/v1/harnesses/hrn-a1b2c3d4e5f6", {
  method: "DELETE",
  headers: {"Authorization": `Bearer ${token}`}
})
```

**Errors:** `404` if the harness does not exist, `409` if a conflicting operation is already pending.

## Errors note

All error responses use the RFC 7807 `ProblemDetails` envelope with `type`, `title`, `status`, `detail`, `instance`, and `extensions` (which includes `request_id` and, for 422 errors, an `errors` array with field paths). See the REST API overview for details.
