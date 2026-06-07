---
slug: api-auth-tokens
title: Auth and API Tokens API
section: reference
summary: REST endpoints for user registration, session login/logout, status probe, and programmatic API token CRUD.
---

Authentication in Primer uses session cookies (for interactive and operator flows) and bearer tokens (for programmatic access). Registration is a one-time bootstrap step that locks after the first user is created.

```ref:concepts/what-is-an-agent
Agents and the sessions that authenticated requests drive.
```

```ref:features/auth-and-tokens
Manage tokens in the console.
```

## Endpoints

| Method | Path | Auth | Summary |
|--------|------|------|---------|
| POST | `/v1/auth/register` | none | Register the first user (locks after first call) |
| POST | `/v1/auth/login` | none | Login and receive a session cookie |
| POST | `/v1/auth/logout` | cookie | Invalidate the current session |
| GET | `/v1/auth/status` | none | Check authentication state |
| POST | `/v1/auth/tokens` | cookie only | Create an API token (plaintext returned once) |
| GET | `/v1/auth/tokens` | cookie only | List the caller's API tokens |
| PUT | `/v1/auth/tokens/{token_id}` | cookie only | Rename an API token |
| DELETE | `/v1/auth/tokens/{token_id}` | cookie only | Revoke an API token |

**Important:** API token management (POST/GET/PUT/DELETE `/v1/auth/tokens`) requires a cookie session. Bearer token credentials cannot mint or manage other tokens; attempting to do so returns `403 token_minting_forbidden`.

## POST /v1/auth/register

Creates the first user. Subsequent calls are rejected with `409`. Registration locks permanently after one user exists.

Request body:

| Field | Required | Constraints | Description |
|-------|----------|-------------|-------------|
| `username` | yes | 1-64 chars | Username for the new account |
| `password` | yes | min 8 chars | Password for the new account |

Returns `200 OK` with `{"username": "<username>"}`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "change-me-now"}'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/auth/register",
    json={"username": "admin", "password": "change-me-now"},
)
# 200 on first call; 409 if a user already exists
--- javascript
const r = await fetch("/v1/auth/register", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({username: "admin", password: "change-me-now"})
})
```

**Errors:** `409` if a user already exists, `422` on validation failure.

## POST /v1/auth/login

Authenticates a user and sets a `session` cookie on the response. Subsequent requests that include this cookie are treated as authenticated.

Request body:

| Field | Required | Description |
|-------|----------|-------------|
| `username` | yes | The registered username |
| `password` | yes | The account password |
| `remember` | no | If `true` (default), the cookie carries `Max-Age` and persists across browser restarts; if `false`, the cookie is session-scoped |

Returns `200 OK` with `{"username": "<username>"}` and sets a `Set-Cookie` header.

```code-tabs:curl,python,javascript
--- curl
curl -c cookies.txt -X POST https://your-host/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "change-me-now"}'
--- python
import httpx
client = httpx.Client(base_url="https://your-host")
r = client.post(
    "/v1/auth/login",
    json={"username": "admin", "password": "change-me-now"},
)
assert r.status_code == 200
# Subsequent requests with this client carry the session cookie automatically
--- javascript
const r = await fetch("/v1/auth/login", {
  method: "POST",
  credentials: "include",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({username: "admin", password: "change-me-now"})
})
```

**Errors:** `401` if credentials are wrong, `422` on validation failure.

## POST /v1/auth/logout

Invalidates the current session cookie. Returns `204 No Content`. Subsequent requests with the same cookie are treated as unauthenticated.

```code-tabs:curl,python,javascript
--- curl
curl -b cookies.txt -X POST https://your-host/v1/auth/logout
--- python
import httpx
r = client.post("/v1/auth/logout")
assert r.status_code == 204
--- javascript
await fetch("/v1/auth/logout", {
  method: "POST",
  credentials: "include"
})
```

## GET /v1/auth/status

Returns the current authentication state without requiring credentials.

```json
{
  "has_user": true,
  "authenticated": true,
  "username": "admin"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `has_user` | boolean | True if at least one user account exists (registration is locked) |
| `authenticated` | boolean | True if the current request carries a valid session cookie |
| `username` | string or null | The logged-in username; null when not authenticated |

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/auth/status
--- python
import httpx
r = httpx.get("https://your-host/v1/auth/status")
status = r.json()
if not status["has_user"]:
    print("Registration is open")
--- javascript
const r = await fetch("/v1/auth/status")
const {has_user, authenticated, username} = await r.json()
```

## POST /v1/auth/tokens

Mints a new API bearer token. The plaintext value is returned **once** in the response and is never retrievable again. Store it securely immediately.

Request body:

| Field | Required | Constraints | Description |
|-------|----------|-------------|-------------|
| `name` | yes | 1-128 chars, unique per user | Human-readable label for the token |
| `scopes` | no | array of strings | Access scopes (empty means full access) |
| `expires_at` | no | ISO 8601, must be in the future | Optional expiry timestamp |

Returns `201 Created` with the token object including `plaintext`.

```json
{
  "id": "at-31d9c8799208",
  "name": "docs-demo-token",
  "prefix": "primer_p",
  "scopes": [],
  "plaintext": "primer_pat_rrrdAxMwXd2eY8Pce0uzUZJ6I9MUA6PaTmvHbpQYM2g",
  "created_at": "2026-06-07T19:00:59.506093Z",
  "expires_at": null
}
```

```code-tabs:curl,python,javascript
--- curl
curl -b cookies.txt -X POST https://your-host/v1/auth/tokens \
  -H "Content-Type: application/json" \
  -d '{"name": "ci-token", "scopes": []}'
--- python
import httpx
# Must use a cookie-authenticated client
r = client.post(
    "/v1/auth/tokens",
    json={"name": "ci-token", "scopes": []},
)
assert r.status_code == 201
token_plaintext = r.json()["plaintext"]  # save this -- never shown again
--- javascript
const r = await fetch("/v1/auth/tokens", {
  method: "POST",
  credentials: "include",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({name: "ci-token", scopes: []})
})
const {plaintext, id} = await r.json()
// Save plaintext immediately -- never retrievable again
```

**Errors:** `403` if the caller authenticated with a bearer token, `409 token_name_conflict` if a token with the same name already exists, `422 token_expires_in_past` if `expires_at` is in the past.

## GET /v1/auth/tokens

Lists all API tokens belonging to the authenticated user. The `plaintext` field is never included in list responses.

```json
{
  "items": [
    {
      "id": "at-31d9c8799208",
      "name": "docs-demo-token",
      "prefix": "primer_p",
      "scopes": [],
      "created_at": "2026-06-07T19:00:59.506093Z",
      "last_used_at": null,
      "expires_at": null,
      "revoked_at": null
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Token identifier (prefix `at-`) |
| `name` | string | Human-readable label |
| `prefix` | string | First characters of the plaintext token (safe to log) |
| `scopes` | array | Access scopes assigned at creation |
| `created_at` | datetime | Creation timestamp |
| `last_used_at` | datetime or null | When the token was last used to authenticate |
| `expires_at` | datetime or null | Expiry time, or null if the token never expires |
| `revoked_at` | datetime or null | Revocation timestamp, or null if still active |

```code-tabs:curl,python,javascript
--- curl
curl -b cookies.txt https://your-host/v1/auth/tokens
--- python
import httpx
r = client.get("/v1/auth/tokens")
tokens = r.json()["items"]
--- javascript
const r = await fetch("/v1/auth/tokens", {credentials: "include"})
const {items} = await r.json()
```

## PUT /v1/auth/tokens/{token_id}

Renames an API token. Returns `200 OK` with the updated `ApiTokenSummary` (no plaintext).

```code-tabs:curl,python,javascript
--- curl
curl -b cookies.txt -X PUT https://your-host/v1/auth/tokens/at-31d9c8799208 \
  -H "Content-Type: application/json" \
  -d '{"name": "ci-token-renamed"}'
--- python
import httpx
r = client.put(
    "/v1/auth/tokens/at-31d9c8799208",
    json={"name": "ci-token-renamed"},
)
updated = r.json()
--- javascript
const r = await fetch("/v1/auth/tokens/at-31d9c8799208", {
  method: "PUT",
  credentials: "include",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({name: "ci-token-renamed"})
})
```

**Errors:** `404` if the token does not exist or belongs to another user, `409 token_name_conflict` if the new name is already taken.

## DELETE /v1/auth/tokens/{token_id}

Revokes an API token. Idempotent: revoking an already-revoked token still returns `204`. Returns `204 No Content`.

```code-tabs:curl,python,javascript
--- curl
curl -b cookies.txt -X DELETE https://your-host/v1/auth/tokens/at-31d9c8799208
--- python
import httpx
r = client.delete("/v1/auth/tokens/at-31d9c8799208")
assert r.status_code == 204
--- javascript
await fetch("/v1/auth/tokens/at-31d9c8799208", {
  method: "DELETE",
  credentials: "include"
})
```

**Errors:** `404` if the token does not exist or belongs to another user.

## Using a bearer token

Once minted, pass the plaintext token in the `Authorization` header for all protected API calls:

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/agents \
  -H "Authorization: Bearer primer_pat_rrrdAxMwXd2eY8Pce0uzUZJ6I9MUA6PaTmvHbpQYM2g"
--- python
import httpx
token = "primer_pat_rrrdAxMwXd2eY8Pce0uzUZJ6I9MUA6PaTmvHbpQYM2g"
r = httpx.get(
    "https://your-host/v1/agents",
    headers={"Authorization": f"Bearer {token}"},
)
--- javascript
const token = "primer_pat_rrrdAxMwXd2eY8Pce0uzUZJ6I9MUA6PaTmvHbpQYM2g"
const r = await fetch("/v1/agents", {
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Errors note

All error responses use the RFC 7807 `ProblemDetails` envelope with `type`, `title`, `status`, `detail`, `instance`, and `extensions` (which includes `request_id` and, for 422 errors, an `errors` array with field paths). See the REST API overview for details.
