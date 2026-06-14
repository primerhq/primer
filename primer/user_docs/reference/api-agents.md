---
slug: api-agents
title: Agents API
section: reference
summary: REST endpoints to create, list, update, and delete agents.
---

An agent is a named, model-backed entity that carries a system prompt, a tool list, and optional compaction instructions. Agents are referenced by id when starting sessions or chats.

```ref:features/agents
What an agent is, the turn loop, and how agents relate to chats and sessions.
```

```ref:features/agents
Create one in the console.
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/agents` | List agents (offset or cursor pagination) |
| POST | `/v1/agents` | Create an agent |
| GET | `/v1/agents/{id}` | Get agent by id |
| PUT | `/v1/agents/{id}` | Replace (full update) an agent |
| DELETE | `/v1/agents/{id}` | Delete an agent |
| POST | `/v1/agents/find` | Filter agents by predicate |
| GET | `/v1/agents/{id}/status` | Validate the agent's external references |

## Agent object

```json
{
  "id": "my-agent",
  "description": "A coding assistant with file access.",
  "model": {
    "provider_id": "anthropic-prod",
    "model_name": "claude-sonnet-4-6"
  },
  "system_prompt": ["You are a senior software engineer.", "Be concise."],
  "compaction_prompt": ["Preserve open tasks and the current file under edit."],
  "tools": ["system__list_files", "system__read_file"],
  "temperature": null,
  "harness_id": null
}
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `id` | no | string | Identifier (case-sensitive). If omitted, the server assigns a type-prefixed id (e.g. `agent-3f9a1c8d`). Immutable after creation |
| `description` | yes | string | Human-readable description |
| `model` | yes | AgentModel | LLM provider and model name (see below) |
| `system_prompt` | no | string[] | Multi-part system prompt; segments joined by the runtime. Empty list means no system prompt |
| `compaction_prompt` | no | string[] | Instructions for conversation compaction. Empty list falls back to the runtime default |
| `tools` | no | string[] | Scoped tool ids in the form `<toolset_id>__<tool_name>`. Empty list means no tools |
| `temperature` | no | number or null | Sampling temperature (>= 0.0). `null` defers to the adapter default |
| `harness_id` | no | string or null | Set by harness management; mutation via CRUD returns 409 when set |

**AgentModel fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `provider_id` | yes | Id of a configured LLMProvider |
| `model_name` | yes | Provider-side model name (e.g. `claude-sonnet-4-6`) |

## Create an agent

`POST /v1/agents` - returns `201 Created`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my-agent",
    "description": "A coding assistant with file access.",
    "model": {"provider_id": "anthropic-prod", "model_name": "claude-sonnet-4-6"},
    "system_prompt": ["You are a senior software engineer.", "Be concise."],
    "tools": ["system__list_files", "system__read_file"]
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/agents",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "my-agent",
        "description": "A coding assistant with file access.",
        "model": {"provider_id": "anthropic-prod", "model_name": "claude-sonnet-4-6"},
        "system_prompt": ["You are a senior software engineer.", "Be concise."],
        "tools": ["system__list_files", "system__read_file"],
    },
)
assert r.status_code == 201
--- javascript
const r = await fetch("/v1/agents", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "my-agent",
    description: "A coding assistant with file access.",
    model: {provider_id: "anthropic-prod", model_name: "claude-sonnet-4-6"},
    system_prompt: ["You are a senior software engineer.", "Be concise."],
    tools: ["system__list_files", "system__read_file"]
  })
})
```

Response `201 Created` - the full agent object as shown above.

**Errors:**
- `422` - validation failed (missing required field, empty `id`, etc.)
- `409` - an agent with this `id` already exists

## Get an agent

`GET /v1/agents/{id}` - returns `200 OK` with the agent object.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/agents/my-agent \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/agents/my-agent",
              headers={"Authorization": f"Bearer {token}"})
--- javascript
const r = await fetch("/v1/agents/my-agent", {
  headers: {"Authorization": `Bearer ${token}`}
})
```

**Errors:** `404` if the id does not exist.

## List agents

`GET /v1/agents` - returns an offset or cursor page of agent objects.

Query parameters: `limit` (1-200, default 20), `offset` (default 0), `cursor`, `order_by`.

```code-tabs:curl,python,javascript
--- curl
curl "https://your-host/v1/agents?limit=50&offset=0" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/agents",
              headers={"Authorization": f"Bearer {token}"},
              params={"limit": 50, "offset": 0})
page = r.json()
agents = page["items"]
--- javascript
const r = await fetch("/v1/agents?limit=50&offset=0", {
  headers: {"Authorization": `Bearer ${token}`}
})
const {items, total, length, offset} = await r.json()
```

## Replace an agent

`PUT /v1/agents/{id}` - full replacement; returns `200 OK` with the updated agent.

The body uses the same schema as `POST`. All fields are replaced; omitted optional fields reset to their defaults.

```code-tabs:curl,python,javascript
--- curl
curl -X PUT https://your-host/v1/agents/my-agent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my-agent",
    "description": "Updated description.",
    "model": {"provider_id": "anthropic-prod", "model_name": "claude-sonnet-4-6"},
    "system_prompt": ["Updated system prompt."],
    "tools": []
  }'
--- python
import httpx
r = httpx.put(
    "https://your-host/v1/agents/my-agent",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "my-agent",
        "description": "Updated description.",
        "model": {"provider_id": "anthropic-prod", "model_name": "claude-sonnet-4-6"},
        "system_prompt": ["Updated system prompt."],
        "tools": [],
    },
)
--- javascript
await fetch("/v1/agents/my-agent", {
  method: "PUT",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "my-agent",
    description: "Updated description.",
    model: {provider_id: "anthropic-prod", model_name: "claude-sonnet-4-6"},
    system_prompt: ["Updated system prompt."],
    tools: []
  })
})
```

**Errors:** `404` if not found, `409` if the agent is managed by a harness.

## Delete an agent

`DELETE /v1/agents/{id}` - returns `204 No Content`. Idempotent: deleting a non-existent id returns `404`.

```code-tabs:curl,python,javascript
--- curl
curl -X DELETE https://your-host/v1/agents/my-agent \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete("https://your-host/v1/agents/my-agent",
                 headers={"Authorization": f"Bearer {token}"})
assert r.status_code == 204
--- javascript
await fetch("/v1/agents/my-agent", {
  method: "DELETE",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Find agents by predicate

`POST /v1/agents/find` - filter, sort, and paginate agents server-side. Returns `200 OK` with an offset or cursor page.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/agents/find \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "predicate": {
      "kind": "predicate",
      "op": "~=",
      "left": {"kind": "field", "name": "id"},
      "right": {"kind": "value", "value": "coding-%"}
    },
    "page": {"kind": "offset", "offset": 0, "length": 20}
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/agents/find",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "predicate": {
            "kind": "predicate", "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": "coding-%"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 20},
    },
)
--- javascript
await fetch("/v1/agents/find", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    predicate: {
      kind: "predicate", op: "~=",
      left: {kind: "field", name: "id"},
      right: {kind: "value", value: "coding-%"}
    },
    page: {kind: "offset", offset: 0, length: 20}
  })
})
```

## Validate agent status

`GET /v1/agents/{id}/status` - checks whether the agent's `model.provider_id` and `model.model_name` resolve against the current provider registry. Returns `200 OK` with a status object.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/agents/my-agent/status \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/agents/my-agent/status",
              headers={"Authorization": f"Bearer {token}"})
--- javascript
const r = await fetch("/v1/agents/my-agent/status", {
  headers: {"Authorization": `Bearer ${token}`}
})
```

**Errors:** `404` if the agent id does not exist.

## Errors note

All error responses use the RFC 7807 `ProblemDetails` envelope with `type`, `title`, `status`, `detail`, `instance`, and `extensions` (which includes `request_id` and, for 422 errors, an `errors` array with field paths). See the REST API overview for details.
