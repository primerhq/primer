---
slug: rest-api-agents-graphs-sessions
title: REST API - agents, graphs, sessions, chats
section: reference
summary: Enumerated endpoints for the compute surface, with the request and response shape per route.
---

## Agents

| Method | Path | What it does |
|---|---|---|
| GET | `/v1/agents` | List agents (pagination via `limit` + `offset`) |
| GET | `/v1/agents/{id}` | Fetch one agent |
| POST | `/v1/agents` | Create an agent |
| PATCH | `/v1/agents/{id}` | Partial update |
| DELETE | `/v1/agents/{id}` | Delete |
| POST | `/v1/agents/find` | Predicate-based search |

Create:

```code-tabs:python,curl,javascript
--- python
client.agents.create(
    name="my-agent",
    model="claude-sonnet-4-6",
    toolsets=["system"],
    system_prompt="...",
)
--- curl
curl -X POST https://primer.example/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name":"my-agent","model":"claude-sonnet-4-6","toolsets":["system"]}'
--- javascript
await fetch("/v1/agents", {
  method: "POST",
  headers: { "Authorization": `Bearer ${token}` },
  body: JSON.stringify({ name: "my-agent", model: "claude-sonnet-4-6", toolsets: ["system"] }),
});
```

## Graphs

| Method | Path | What it does |
|---|---|---|
| GET | `/v1/graphs` | List graphs |
| POST | `/v1/graphs` | Create a graph definition |
| POST | `/v1/graphs/{id}/runs` | Start a graph run |
| GET | `/v1/graphs/{id}/runs/{run_id}` | Read run state |

## Sessions

| Method | Path | What it does |
|---|---|---|
| GET | `/v1/sessions` | List sessions |
| GET | `/v1/sessions/{id}` | Detail (includes transcript) |
| POST | `/v1/sessions` | Create a session against an agent |
| POST | `/v1/sessions/{id}/turn` | Drive one turn manually |
| POST | `/v1/sessions/{id}/pause` | Pause |
| POST | `/v1/sessions/{id}/resume` | Resume |
| POST | `/v1/sessions/{id}/cancel` | Cancel |
| POST | `/v1/sessions/find` | Predicate-based search |

## Chats

| Method | Path | What it does |
|---|---|---|
| GET | `/v1/chats` | List chats |
| GET | `/v1/chats/{id}` | Detail (includes messages) |
| POST | `/v1/chats` | Create a chat |
| POST | `/v1/chats/{id}/messages` | Send a message |
| GET (ws) | `/v1/chats/{id}/ws` | Stream messages |

```callout:tip
The schema for every request and response body is in the
auto-generated OpenAPI document at `/v1/openapi.json`. Point
your code generator at the running server rather than copying
a snapshot.
```

## Error envelope

Every error response:

```
{
  "detail": {
    "error": "<kind>",
    "message": "<human-readable>",
    "fields": { ... }   # only for 422 validation errors
  }
}
```
