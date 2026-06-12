---
slug: api-chats
title: API Reference - Chats
summary: Complete endpoint reference for the Chats surface, including create, list, messages, WebSocket stream, and tool-approval endpoints.
section: reference
---

Chats are user-driven conversations with a single agent, persisted as top-level entities (not nested under a workspace). Each chat runs over a WebSocket stream and can park mid-turn on yielding tools, including the `_approval` gate.

```ref:concepts/chats
```

```ref:features/chats
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| POST | `/v1/chats` | Create a new chat bound to an agent |
| POST | `/v1/chats/{chat_id}/agent` | Switch the chat's current agent |
| GET | `/v1/chats` | List chats (paginated) |
| GET | `/v1/chats/{chat_id}` | Get a chat by id |
| DELETE | `/v1/chats/{chat_id}` | End (or hard-delete) a chat |
| GET | `/v1/chats/{chat_id}/messages` | List messages on a chat |
| GET (ws) | `/v1/chats/{chat_id}/ws` | Stream messages and events over WebSocket |

---

## POST /v1/chats

Creates a new chat. The agent reference is validated at creation time; a missing agent returns `404`.

**Request body**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `agent_id` | string | yes | Id of an existing agent |

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://primer.example/v1/chats \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent-assistant"}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/chats",
    headers={"Authorization": f"Bearer {token}"},
    json={"agent_id": "agent-assistant"},
)
r.raise_for_status()
chat = r.json()
--- javascript
const r = await fetch("/v1/chats", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ agent_id: "agent-assistant" }),
});
const chat = await r.json();
```

**Response: 201**

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Server-assigned id, prefixed `chat-` |
| `agent_id` | string | Echoed from request |
| `status` | string | `active` or `ended` |
| `last_seq` | integer | Starts at `0`; increments per message |
| `title` | string or null | Set on the first user turn; null until then |
| `created_at` | string | ISO-8601 timestamp |

**Errors:** `404` agent not found, `422` missing `agent_id`.

---

## POST /v1/chats/{chat_id}/agent

Switches the chat's current agent mid-conversation. The agent and its system
prompt are resolved fresh at the start of every turn from `chat.agent_id`, so
updating that field is the entire switch: the next turn runs under the new
agent with the full prior conversation as shared context. History is never
rewritten - only the system prompt and tool set change from the next turn on.

If the chat is paused on a pending gate (an `ask_user` question or a tool
`_approval`), that gate is auto-rejected before the switch so the append-only
message log stays valid: a rejected `tool_result` plus a `cancelled` row are
appended and `pending_tool_call` is cleared. Switching to the agent the chat is
already bound to is an idempotent no-op that returns the unchanged chat.

**Request body**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `agent_id` | string | yes | Id of the agent to switch this chat to |

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://primer.example/v1/chats/chat-abc123/agent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent-researcher"}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/chats/chat-abc123/agent",
    headers={"Authorization": f"Bearer {token}"},
    json={"agent_id": "agent-researcher"},
)
r.raise_for_status()
chat = r.json()
--- javascript
const r = await fetch("/v1/chats/chat-abc123/agent", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ agent_id: "agent-researcher" }),
});
const chat = await r.json();
```

**Response: 200** - the updated `Chat` object, with `agent_id` set to the new
agent. Switching to the same agent returns the chat unchanged (idempotent).

**Errors:** `404` chat not found or target agent not found, `409` the chat has
ended, `422` missing or empty `agent_id`.

---

## GET /v1/chats

Lists chats with optional agent filter and pagination.

**Query parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `agent_id` | string | Filter to one agent |
| `limit` | integer | Page size |
| `offset` | integer | Page offset |
| `cursor` | string | Cursor token for cursor-mode pagination |

```code-tabs:curl,python,javascript
--- curl
curl "https://primer.example/v1/chats?agent_id=agent-assistant&limit=20&offset=0" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/chats",
    headers={"Authorization": f"Bearer {token}"},
    params={"agent_id": "agent-assistant", "limit": 20, "offset": 0},
)
r.raise_for_status()
page = r.json()
--- javascript
const params = new URLSearchParams({
  agent_id: "agent-assistant", limit: 20, offset: 0,
});
const r = await fetch(`/v1/chats?${params}`, {
  headers: { "Authorization": `Bearer ${token}` },
});
const page = await r.json();
```

**Response: 200** - `{"items": [Chat, ...], "total": N}`

---

## GET /v1/chats/{chat_id}

Returns a single chat by id.

```code-tabs:curl,python,javascript
--- curl
curl "https://primer.example/v1/chats/chat-abc123" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/chats/chat-abc123",
    headers={"Authorization": f"Bearer {token}"},
)
r.raise_for_status()
chat = r.json()
--- javascript
const r = await fetch("/v1/chats/chat-abc123", {
  headers: { "Authorization": `Bearer ${token}` },
});
const chat = await r.json();
```

**Response: 200** - `Chat` object. **Errors:** `404` not found.

---

## DELETE /v1/chats/{chat_id}

Ends a chat (soft delete by default) by transitioning it to `status: "ended"`. A second `DELETE` on an already-ended chat returns `409`. Pass `?force=true` to hard-delete the row and all messages.

**Response: 200** - `Chat` with `status: "ended"`. **Errors:** `404` not found, `409` already ended.

---

## GET /v1/chats/{chat_id}/messages

Lists stored messages for a chat. Returns `404` if the chat does not exist (probe-resistance: the endpoint does not reveal whether an id has zero messages vs. does not exist).

**Query parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `after_seq` | integer | Return only messages with `seq > after_seq` |
| `before_seq` | integer | Return only messages with `seq < before_seq` |
| `limit` | integer | Page size |
| `offset` | integer | Page offset |
| `cursor` | string | Cursor token for cursor-mode pagination |

```code-tabs:curl,python,javascript
--- curl
curl "https://primer.example/v1/chats/chat-abc123/messages?after_seq=0&limit=50" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/chats/chat-abc123/messages",
    headers={"Authorization": f"Bearer {token}"},
    params={"after_seq": 0, "limit": 50},
)
r.raise_for_status()
page = r.json()
--- javascript
const params = new URLSearchParams({ after_seq: 0, limit: 50 });
const r = await fetch(`/v1/chats/chat-abc123/messages?${params}`, {
  headers: { "Authorization": `Bearer ${token}` },
});
const page = await r.json();
```

**Response: 200** - `{"items": [...], "total": N}`. **Errors:** `404` chat not found.

---

## GET (ws) /v1/chats/{chat_id}/ws

WebSocket endpoint for streaming chat messages and events in real time. Connect once and receive all messages appended after the supplied cursor.

**Query parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `cursor` | integer | Resume from this `seq` value; messages with `seq > cursor` are replayed then streamed live. Defaults to `0` (full replay). |

Authentication uses the same bearer token or session cookie as the REST surface. Pass it via a query param or a first-frame protocol message depending on your WebSocket client.

The server emits JSON frames per message. On reconnect, supply the highest `seq` you received as `?cursor=<seq>` to avoid replaying already-seen messages.

**Example connection (pseudocode)**

```code-tabs:curl,python,javascript
--- curl
# wscat (npm i -g wscat):
wscat -c "wss://primer.example/v1/chats/chat-abc123/ws?cursor=0" \
  -H "Authorization: Bearer $TOKEN"
--- python
import asyncio
import httpx
import websockets

async def stream():
    uri = "wss://primer.example/v1/chats/chat-abc123/ws?cursor=0"
    async with websockets.connect(
        uri, extra_headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        async for frame in ws:
            print(frame)

asyncio.run(stream())
--- javascript
const ws = new WebSocket(
  "/v1/chats/chat-abc123/ws?cursor=0",
);
ws.addEventListener("message", (evt) => {
  const frame = JSON.parse(evt.data);
  console.log(frame);
});
```

---

## Error envelopes

All error responses use RFC 7807 problem details:

```code-tabs:curl,python,javascript
--- curl
# Example 404 response body:
# {
#   "type": "/errors/not-found",
#   "title": "Not Found",
#   "status": 404,
#   "detail": "chat not found"
# }
--- python
# Inspect r.json()["type"] after a non-2xx response:
# "/errors/not-found"        -- chat or agent does not exist
# "/errors/conflict"         -- illegal state transition (e.g. DELETE on ended chat)
# "/errors/validation-error" -- missing required field (e.g. no agent_id)
--- javascript
// On error, r.ok === false and r.json() returns:
// { type: "/errors/not-found", title: "...", status: 404, detail: "..." }
// Common types: /errors/not-found, /errors/conflict, /errors/validation-error
```
