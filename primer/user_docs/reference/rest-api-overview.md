---
slug: rest-api-overview
title: REST API overview
section: reference
summary: Authentication, base URL, error envelope (RFC 7807), and pagination for every /v1 endpoint.
---

All API routes are under `/v1`. The OpenAPI schema is served at `/v1/openapi.json`; the Swagger UI at `/v1/docs`.

```ref:getting-started/introduction
Introduction and quick-start setup.
```

```ref:features/agents
Agents, the turn loop, and core concepts before using the API.
```

## Authentication

**Register a new user account**

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "s3cret"}'
--- python
import httpx
r = httpx.post("https://your-host/v1/auth/register",
               json={"username": "alice", "password": "s3cret"})
# Sets a session cookie automatically on 200
--- javascript
await fetch("/v1/auth/register", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({username: "alice", password: "s3cret"})
})
```

Response `200 OK`:

```json
{"username": "alice"}
```

**Log in**

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "s3cret", "remember": false}'
--- python
import httpx
r = httpx.post("https://your-host/v1/auth/login",
               json={"username": "alice", "password": "s3cret"})
--- javascript
await fetch("/v1/auth/login", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({username: "alice", password: "s3cret"})
})
```

A successful login sets a session cookie. For programmatic access use **bearer tokens** (see below).

**Bearer tokens**

Create a long-lived token once; pass it on every request instead of a session cookie.

Token CRUD is operator-only: it requires an active **session cookie**. A request authenticated with a bearer token is rejected with `403` - bearer credentials cannot mint or manage other tokens.

```code-tabs:curl,python,javascript
--- curl
# Create a token (requires an active session cookie; cookie sent via -b)
curl -X POST https://your-host/v1/auth/tokens \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"name": "ci-pipeline", "scopes": []}'
--- python
import httpx
r = httpx.post("https://your-host/v1/auth/tokens",
               cookies=session_cookies,
               json={"name": "ci-pipeline", "scopes": []})
print(r.json()["plaintext"])   # shown ONCE; store it securely
--- javascript
const r = await fetch("/v1/auth/tokens", {
  method: "POST",
  credentials: "include",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({name: "ci-pipeline", scopes: []})
})
const {plaintext} = await r.json()   // shown ONCE
```

Response `201 Created`:

```json
{
  "id": "at-01abc...",
  "name": "ci-pipeline",
  "prefix": "primer_p",
  "plaintext": "primer_pat_...",
  "scopes": [],
  "created_at": "2025-01-15T09:00:00Z",
  "expires_at": null
}
```

The `plaintext` field is returned **once only**. Store it securely; subsequent `GET /v1/auth/tokens` returns only `ApiTokenSummary` objects (no `plaintext`).

**Auth endpoints**

| Method | Path | Summary |
|--------|------|---------|
| POST | `/v1/auth/register` | Register a new user account |
| POST | `/v1/auth/login` | Log in, receive session cookie |
| POST | `/v1/auth/logout` | Invalidate the current session |
| GET | `/v1/auth/status` | Check who is logged in |
| GET | `/v1/auth/tokens` | List your API tokens (plaintext omitted) |
| POST | `/v1/auth/tokens` | Create a new API token (plaintext returned once) |
| PUT | `/v1/auth/tokens/{token_id}` | Rename a token |
| DELETE | `/v1/auth/tokens/{token_id}` | Revoke a token (idempotent) |

## Error envelope (RFC 7807)

Every non-2xx response uses the `ProblemDetails` shape:

```json
{
  "type": "/errors/validation-error",
  "title": "Validation Error",
  "status": 422,
  "detail": "Field 'model' is required.",
  "instance": "/v1/agents",
  "extensions": {
    "request_id": "req_01abc...",
    "errors": [
      {"loc": ["body", "model"], "msg": "Field required", "type": "missing"}
    ]
  }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `type` | string | URI identifying the problem type, e.g. `/errors/validation-error`, `/errors/not-found`, `/errors/conflict` |
| `title` | string | Short human-readable summary |
| `status` | integer | HTTP status code |
| `detail` | string | Human-readable explanation of this occurrence |
| `instance` | string (nullable) | Request URI where the error originated |
| `extensions` | object (nullable) | Extra fields per RFC 7807 section 3.2; always includes `request_id` when available |

Common `type` values:

| type | HTTP status | Meaning |
|------|-------------|---------|
| `/errors/validation-error` | 422 | Pydantic validation failed; `extensions.errors` lists field paths |
| `/errors/not-found` | 404 | Entity with the given id does not exist |
| `/errors/conflict` | 409 | Duplicate id or other conflict |
| `/errors/internal` | 500 | Unexpected server error |

## Pagination

List endpoints accept query parameters `limit` (1-200, default 20), `offset` (default 0), and optionally `cursor` and `order_by`.

**Offset response envelope:**

```json
{
  "kind": "offset",
  "offset": 0,
  "length": 3,
  "total": 42,
  "items": [...]
}
```

- `total` is always an integer for CRUD entities (not null).
- `length` equals `len(items)` including on partial last pages.
- `total` reflects the filtered set, not the full table, when a predicate is active.

**Cursor response envelope:**

```json
{
  "kind": "cursor",
  "next_cursor": "eyJpZCI6ICJhYmMi...",
  "items": [...]
}
```

Cursor responses do **not** include `total`.

Boundary rules (enforced with a 422 error):
- `limit` / `length` minimum: 1
- `limit` maximum on GET list endpoints: 200
- `length` maximum on `/find` POST endpoints: 200

## The `find` predicate endpoint

Every CRUD resource exposes `POST /v1/<resource>/find` in addition to the plain `GET` list. Use it to filter, sort, and paginate server-side.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/toolsets/find \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "predicate": {
      "kind": "predicate",
      "op": "~=",
      "left": {"kind": "field", "name": "id"},
      "right": {"kind": "value", "value": "my-prefix%"}
    },
    "page": {"kind": "offset", "offset": 0, "length": 20}
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/toolsets/find",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "predicate": {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": "my-prefix%"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 20},
    },
)
--- javascript
await fetch("/v1/toolsets/find", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    predicate: {
      kind: "predicate", op: "~=",
      left: {kind: "field", name: "id"},
      right: {kind: "value", value: "my-prefix%"}
    },
    page: {kind: "offset", offset: 0, length: 20}
  })
})
```

`FindRequest` fields:

| Field | Required | Description |
|-------|----------|-------------|
| `page` | yes | Pagination: `{"kind": "offset", "offset": 0, "length": N}` or `{"kind": "cursor", "cursor": null, "length": N}` |
| `predicate` | no | Filter predicate; `null` returns all entities |
| `order_by` | no | Array of sort keys |

The `~=` operator performs a SQL `LIKE` match (use `%` as wildcard). Pass `predicate: null` to list all entities with pagination.
