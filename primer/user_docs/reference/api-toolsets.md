---
slug: api-toolsets
title: Toolsets API
section: reference
summary: REST endpoints to create, list, update, delete, and introspect toolsets, including MCP stdio and HTTP transports.
---

A toolset is a named collection of tools sourced from either the internal registry or a connected MCP server. Agents reference toolsets by scoped tool ids of the form `<toolset_id>__<tool_name>`.

```ref:concepts/toolsets-and-tools
What toolsets are and how tools are scoped.
```

```ref:features/mcp-server
Connecting an MCP server as a toolset source.
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/toolsets` | List toolsets (offset or cursor pagination) |
| POST | `/v1/toolsets` | Create a toolset |
| GET | `/v1/toolsets/{id}` | Get toolset by id |
| PUT | `/v1/toolsets/{id}` | Replace (full update) a toolset |
| DELETE | `/v1/toolsets/{id}` | Delete a toolset |
| POST | `/v1/toolsets/find` | Filter toolsets by predicate |
| GET | `/v1/toolsets/builtin` | List built-in (internal) toolsets |
| GET | `/v1/toolsets/{id}/tools` | Enumerate live tools exposed by a toolset |
| POST | `/v1/toolsets/{id}/invalidate` | Invalidate the cached toolset provider |

## Toolset object

```json
{
  "id": "my-mcp-toolset",
  "provider": "mcp",
  "config": {
    "transport": "stdio",
    "config": {
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {}
    }
  },
  "harness_id": null
}
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `id` | yes | string | User-defined identifier (min length 1, case-sensitive) |
| `provider` | yes | string | `"internal"` or `"mcp"` |
| `config` | conditional | McpConfig or null | Required when `provider` is `"mcp"`. Must be omitted for `"internal"` |
| `harness_id` | no | string or null | Set by harness management; mutation via CRUD returns 409 when set |

**McpConfig fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `transport` | yes | `"stdio"` or `"http"` |
| `config` | yes | Transport-specific details; must match `transport` |

**StdioConfig fields** (when `transport` is `"stdio"`):

| Field | Required | Description |
|-------|----------|-------------|
| `command` | yes | Argv list to launch the MCP server subprocess (min 1 element) |
| `env` | no | Environment variables to set when launching the subprocess |

**HttpConfig fields** (when `transport` is `"http"`):

| Field | Required | Description |
|-------|----------|-------------|
| `url` | yes | Base URL of the remote MCP server endpoint (min length 1) |
| `headers` | no | HTTP headers included on every request (e.g. `Authorization`) |
| `oauth` | no | OAuthConfig for OAuth 2.1 (PKCE) flows; overrides any `Authorization` header |

## Stdio allowlist

For `transport: stdio`, the MCP provider checks `command[0]` against a server-configured allowlist at the point of first use (not at row create). A command not in the allowlist causes `GET /v1/toolsets/{id}/tools` to return `503 /errors/service-unavailable` with a detail message naming the rejected command. The default allowlist contains `npx`, `python`, and `uv`.

The POST succeeds regardless; the rejection is enforced lazily when the MCP session is first opened.

## Create a toolset

`POST /v1/toolsets` - returns `201 Created`.

**Stdio MCP toolset:**

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/toolsets \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "fs-tools",
    "provider": "mcp",
    "config": {
      "transport": "stdio",
      "config": {
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
      }
    }
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/toolsets",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "fs-tools",
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {
                "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            },
        },
    },
)
assert r.status_code == 201
--- javascript
const r = await fetch("/v1/toolsets", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "fs-tools",
    provider: "mcp",
    config: {
      transport: "stdio",
      config: {
        command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
      }
    }
  })
})
```

**HTTP MCP toolset:**

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/toolsets \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "remote-mcp",
    "provider": "mcp",
    "config": {
      "transport": "http",
      "config": {
        "url": "https://mcp.example.com/v1",
        "headers": {"Authorization": "Bearer mcp-token"}
      }
    }
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/toolsets",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "remote-mcp",
        "provider": "mcp",
        "config": {
            "transport": "http",
            "config": {
                "url": "https://mcp.example.com/v1",
                "headers": {"Authorization": "Bearer mcp-token"},
            },
        },
    },
)
assert r.status_code == 201
--- javascript
const r = await fetch("/v1/toolsets", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "remote-mcp",
    provider: "mcp",
    config: {
      transport: "http",
      config: {
        url: "https://mcp.example.com/v1",
        headers: {Authorization: "Bearer mcp-token"}
      }
    }
  })
})
```

Response `201 Created` - the full toolset object.

**Errors:**
- `409` - a toolset with this `id` already exists
- `422` - validation failed (e.g. `transport` and inner config shape mismatch, empty `id`, integer `id`)

## Get a toolset

`GET /v1/toolsets/{id}` - returns `200 OK` with the toolset object.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/toolsets/fs-tools \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/toolsets/fs-tools",
              headers={"Authorization": f"Bearer {token}"})
--- javascript
const r = await fetch("/v1/toolsets/fs-tools", {
  headers: {"Authorization": `Bearer ${token}`}
})
```

**Errors:** `404` if the id does not exist.

## List toolsets

`GET /v1/toolsets` - returns an offset or cursor page of toolset objects.

Query parameters: `limit` (1-200, default 20), `offset` (default 0), `cursor`, `order_by`.

```code-tabs:curl,python,javascript
--- curl
curl "https://your-host/v1/toolsets?limit=50&offset=0" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/toolsets",
              headers={"Authorization": f"Bearer {token}"},
              params={"limit": 50, "offset": 0})
page = r.json()
toolsets = page["items"]
--- javascript
const r = await fetch("/v1/toolsets?limit=50&offset=0", {
  headers: {"Authorization": `Bearer ${token}`}
})
const {items, total, length, offset} = await r.json()
```

## Replace a toolset

`PUT /v1/toolsets/{id}` - full replacement; returns `200 OK` with the updated toolset.

The body uses the same schema as `POST`. All fields are replaced; omitted optional fields reset to their defaults.

```code-tabs:curl,python,javascript
--- curl
curl -X PUT https://your-host/v1/toolsets/fs-tools \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "fs-tools",
    "provider": "mcp",
    "config": {
      "transport": "stdio",
      "config": {
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"]
      }
    }
  }'
--- python
import httpx
r = httpx.put(
    "https://your-host/v1/toolsets/fs-tools",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "fs-tools",
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"]},
        },
    },
)
--- javascript
await fetch("/v1/toolsets/fs-tools", {
  method: "PUT",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "fs-tools",
    provider: "mcp",
    config: {
      transport: "stdio",
      config: {command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"]}
    }
  })
})
```

**Errors:** `404` if not found, `409` if the toolset is managed by a harness.

## Delete a toolset

`DELETE /v1/toolsets/{id}` - returns `204 No Content`. Agents that reference this toolset by scoped tool id are not blocked from existing; their `/status` endpoint flips to `ok=false` until the toolset is recreated.

```code-tabs:curl,python,javascript
--- curl
curl -X DELETE https://your-host/v1/toolsets/fs-tools \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete("https://your-host/v1/toolsets/fs-tools",
                 headers={"Authorization": f"Bearer {token}"})
assert r.status_code == 204
--- javascript
await fetch("/v1/toolsets/fs-tools", {
  method: "DELETE",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## List tools exposed by a toolset

`GET /v1/toolsets/{id}/tools` - connects to the live MCP provider and returns the current tool list. Returns `200 OK` with the tool enumeration.

For OAuth-protected HTTP toolsets, a `401` response includes an `extensions.auth_url` field for the user consent flow.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/toolsets/fs-tools/tools \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/toolsets/fs-tools/tools",
              headers={"Authorization": f"Bearer {token}"})
tools = r.json()
--- javascript
const r = await fetch("/v1/toolsets/fs-tools/tools", {
  headers: {"Authorization": `Bearer ${token}`}
})
const tools = await r.json()
```

**Errors:**
- `401` - OAuth consent required; `extensions.auth_url` contains the redirect URL
- `404` - toolset id not found
- `503` - stdio command not in the server allowlist (`/errors/service-unavailable`)
- `502` - the MCP server returned an error
- `504` - network timeout reaching the MCP server

## Errors note

All error responses use the RFC 7807 `ProblemDetails` envelope with `type`, `title`, `status`, `detail`, `instance`, and `extensions` (which includes `request_id` and, for 422 errors, an `errors` array with field paths). See the REST API overview for details.
