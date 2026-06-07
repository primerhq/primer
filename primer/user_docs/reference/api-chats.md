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
| GET | `/v1/chats` | List chats (paginated) |
| GET | `/v1/chats/{chat_id}` | Get a chat by id |
| DELETE | `/v1/chats/{chat_id}` | End (or hard-delete) a chat |
| GET | `/v1/chats/{chat_id}/messages` | List messages on a chat |
| GET (ws) | `/v1/chats/{chat_id}/ws` | Stream messages and events over WebSocket |
| GET | `/v1/chats/{chat_id}/tool_approval/pending` | Get pending tool approval request |
| POST | `/v1/chats/{chat_id}/tool_approval/respond` | Submit an approval decision |

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
| `parked_status` | string or null | `parked`, `resumable`, or null |

**Errors:** `404` agent not found, `422` missing `agent_id`.

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

## GET /v1/chats/{chat_id}/tool_approval/pending

Returns the pending tool approval request when the chat is parked on the `_approval` tool. Returns `404` when the chat is not parked, or is parked on a different tool.

```code-tabs:curl,python,javascript
--- curl
curl "https://primer.example/v1/chats/chat-abc123/tool_approval/pending" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/chats/chat-abc123/tool_approval/pending",
    headers={"Authorization": f"Bearer {token}"},
)
--- javascript
const r = await fetch("/v1/chats/chat-abc123/tool_approval/pending", {
  headers: { "Authorization": `Bearer ${token}` },
});
```

**Response: 200**

| Field | Type | Notes |
|-------|------|-------|
| `tool_call_id` | string | Required for the respond call |
| `tool_name` | string | Inner tool the agent tried to call |
| `arguments` | object | Arguments passed to the inner tool |
| `parked_at` | string | ISO-8601 timestamp when the chat parked |
| `timeout_at` | string or null | ISO-8601 expiry derived from `parked_at` + policy timeout |
| `policy_id` | string or null | Approval policy that triggered the gate |
| `approval_type` | string or null | e.g. `required` |
| `gate_reason` | string or null | Human-readable reason from the policy |

**Errors:** `404` not parked on `_approval`.

---

## POST /v1/chats/{chat_id}/tool_approval/respond

Submits an approval decision for the pending tool call. A `tool_call_id` mismatch returns `404`; the parked id is never echoed in the error response.

**Request body**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `tool_call_id` | string | yes | Must match the value from `/pending` |
| `decision` | string | yes | `approved` or `rejected` |
| `reason` | string | no | Optional free-text reason (max 1024 chars) |

```code-tabs:curl,python,javascript
--- curl
curl -X POST "https://primer.example/v1/chats/chat-abc123/tool_approval/respond" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool_call_id": "tc-shell-007", "decision": "rejected", "reason": "not permitted"}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/chats/chat-abc123/tool_approval/respond",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "tool_call_id": "tc-shell-007",
        "decision": "rejected",
        "reason": "not permitted",
    },
)
r.raise_for_status()
--- javascript
const r = await fetch("/v1/chats/chat-abc123/tool_approval/respond", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    tool_call_id: "tc-shell-007",
    decision: "rejected",
    reason: "not permitted",
  }),
});
```

**Response: 202** - `{"status": "accepted"}`. **Errors:** `404` if `tool_call_id` does not match the parked yield.

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
