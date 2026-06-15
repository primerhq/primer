---
slug: api-sessions
title: API Reference - Sessions
summary: Complete endpoint reference for the Sessions surface, including lifecycle signals, predicate search, YIELD endpoints (ask_user, yields cancel, tool approval), and RFC 7807 error shapes.
section: reference
---

Sessions represent a long-running agent or graph execution attached to a workspace. Each session progresses through a defined lifecycle and can park mid-turn to await operator input via yielding-tool endpoints.

```ref:workspaces/workspaces-and-sessions
Session lifecycle, statuses, and the session detail view.
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| POST | `/v1/workspaces/{workspace_id}/sessions` | Create a session |
| GET | `/v1/workspaces/{workspace_id}/sessions` | List sessions on a workspace |
| GET | `/v1/workspaces/{workspace_id}/sessions/{session_id}` | Get session state |
| DELETE | `/v1/workspaces/{workspace_id}/sessions/{session_id}` | Hard-delete a session |
| POST | `/v1/workspaces/{workspace_id}/sessions/{session_id}/resume` | Start or resume (idempotent) |
| POST | `/v1/workspaces/{workspace_id}/sessions/{session_id}/pause` | Soft pause request |
| POST | `/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel` | Hard cancel to ENDED |
| POST | `/v1/workspaces/{workspace_id}/sessions/{session_id}/steer` | Append a steering instruction |
| GET | `/v1/sessions` | List sessions across all workspaces |
| GET | `/v1/sessions/{session_id}` | Get session by id (no workspace context) |
| POST | `/v1/sessions/find` | Predicate-based search with cursor or offset paging |
| GET | `/v1/sessions/{session_id}/ask_user/pending` | Get pending ask_user prompt |
| POST | `/v1/sessions/{session_id}/ask_user/respond` | Submit response to ask_user |
| POST | `/v1/sessions/{session_id}/yields/{tool_call_id}/cancel` | Cancel one in-flight yield |
| GET | `/v1/sessions/{session_id}/tool_approval/pending` | Get pending tool approval request |
| POST | `/v1/sessions/{session_id}/tool_approval/respond` | Submit an approval decision |

---

## POST /v1/workspaces/{workspace_id}/sessions

Creates a session and optionally starts it immediately.

**Request body**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `binding` | object | yes | Discriminated union: `{"kind":"agent","agent_id":"..."}` or `{"kind":"graph","graph_id":"..."}` |
| `auto_start` | boolean | no | Default `false`. When `true`, transitions immediately to `running` |
| `initial_instructions` | string | no | Prepended to the first turn as a user instruction |
| `parent_session_id` | string | no | Links this session as a child for hierarchical filtering |
| `metadata` | object | no | Arbitrary key-value blob stored on the row |
| `graph_input` | any | no | Input value for graph bindings only |

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://primer.example/v1/workspaces/ws-prod/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "binding": {"kind": "agent", "agent_id": "agent-coder"},
    "auto_start": true,
    "initial_instructions": "Implement the described feature"
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspaces/ws-prod/sessions",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "binding": {"kind": "agent", "agent_id": "agent-coder"},
        "auto_start": True,
        "initial_instructions": "Implement the described feature",
    },
)
r.raise_for_status()
session = r.json()
--- javascript
const r = await fetch("/v1/workspaces/ws-prod/sessions", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    binding: { kind: "agent", agent_id: "agent-coder" },
    auto_start: true,
    initial_instructions: "Implement the described feature",
  }),
});
const session = await r.json();
```

**Response: 201**

Returns a `WorkspaceSession` object. Key fields:

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Server-assigned session id |
| `workspace_id` | string | Parent workspace |
| `binding` | object | Echoes the supplied binding with `kind`, `agent_id` or `graph_id` |
| `status` | string | `created`, `running`, `waiting`, `paused`, or `ended` |
| `turn_no` | integer | Increments on each turn |
| `parked_status` | string or null | `parked`, `resumable`, or null |
| `ended_reason` | string or null | `completed`, `failed`, `cancelled`, `workspace_lost`, `force_deleted` |
| `ended_at` | string or null | ISO-8601 timestamp when the session ended |
| `metadata` | object | Echoed from create body |
| `created_at` | string | ISO-8601 creation timestamp |

**Errors:** `404` workspace not found, `409` conflict (e.g. workspace in wrong state), `422` validation.

---

## GET /v1/workspaces/{workspace_id}/sessions

Lists sessions on a specific workspace. Supports offset pagination via `limit` and `offset` query params.

**Query parameters:** `limit`, `offset`, `order_by` (e.g. `created_at:desc`).

**Response: 200** - `{"items": [WorkspaceSession, ...], "total": N}`

---

## GET /v1/sessions

Lists sessions across all workspaces with optional filtering.

**Query parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `workspace_id` | string | Filter to one workspace |
| `agent_id` | string | Filter to sessions with a matching agent binding; graph-bound sessions are never returned |
| `graph_id` | string | Filter to sessions with a matching graph binding; agent-bound sessions are never returned |
| `status` | string | One of `created`, `running`, `waiting`, `paused`, `ended` |
| `parent_session_id` | string | Direct-parent filter (non-transitive) |
| `order_by` | string | e.g. `created_at:asc` or `created_at:desc` |
| `limit` | integer | Page size |
| `offset` | integer | Page offset |

All filter params combine with AND semantics. A missing graph or agent produces an empty list, not a 404.

```code-tabs:curl,python,javascript
--- curl
curl "https://primer.example/v1/sessions?workspace_id=ws-prod&status=running&limit=20&offset=0" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/sessions",
    headers={"Authorization": f"Bearer {token}"},
    params={"workspace_id": "ws-prod", "status": "running", "limit": 20, "offset": 0},
)
r.raise_for_status()
page = r.json()
--- javascript
const params = new URLSearchParams({
  workspace_id: "ws-prod", status: "running", limit: 20, offset: 0,
});
const r = await fetch(`/v1/sessions?${params}`, {
  headers: { "Authorization": `Bearer ${token}` },
});
const page = await r.json();
```

**Response: 200** - `{"items": [WorkspaceSession, ...], "total": N}`

---

## GET /v1/sessions/{session_id}

Fetches a single session by id without requiring a workspace URL prefix. Returns `404` if not found.

```code-tabs:curl,python,javascript
--- curl
curl "https://primer.example/v1/sessions/sess-abc123" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/sessions/sess-abc123",
    headers={"Authorization": f"Bearer {token}"},
)
r.raise_for_status()
session = r.json()
--- javascript
const r = await fetch("/v1/sessions/sess-abc123", {
  headers: { "Authorization": `Bearer ${token}` },
});
const session = await r.json();
```

**Response: 200** - `WorkspaceSession` object. **Errors:** `404` not found.

---

## POST /v1/sessions/find

Predicate-based search with cursor or offset pagination and optional ordering.

**Request body**

```code-tabs:curl,python,javascript
--- curl
curl -X POST "https://primer.example/v1/sessions/find" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "predicate": {
      "kind": "predicate",
      "op": "=",
      "left": {"kind": "field", "name": "workspace_id"},
      "right": {"kind": "value", "value": "ws-prod"}
    },
    "page": {"kind": "offset", "offset": 0, "length": 25},
    "order_by": [{"field": "created_at", "direction": "desc"}]
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/sessions/find",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "predicate": {
            "kind": "predicate",
            "op": "=",
            "left": {"kind": "field", "name": "workspace_id"},
            "right": {"kind": "value", "value": "ws-prod"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 25},
        "order_by": [{"field": "created_at", "direction": "desc"}],
    },
)
r.raise_for_status()
result = r.json()
--- javascript
const r = await fetch("/v1/sessions/find", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    predicate: {
      kind: "predicate", op: "=",
      left: { kind: "field", name: "workspace_id" },
      right: { kind: "value", value: "ws-prod" },
    },
    page: { kind: "offset", offset: 0, length: 25 },
    order_by: [{ field: "created_at", direction: "desc" }],
  }),
});
const result = await r.json();
```

**Cursor mode:** pass `"page": {"kind": "cursor", "cursor": null, "length": 25}` for the first page. Subsequent pages use `cursor` from the `next_cursor` field in the response. Cursor pagination is stable and complete: every matching session appears exactly once across the full walk, even when multiple sessions share the same timestamp. The cursor always includes the session `id` as a stable tiebreaker, so rows with identical sort-key values are never skipped or duplicated at page boundaries.

**Response: 200** - For offset pages: `{"kind": "offset", "items": [...], "total": N}`. For cursor pages: `{"kind": "cursor", "items": [...], "next_cursor": "..."}` (null `next_cursor` means last page).

---

## POST /v1/workspaces/{workspace_id}/sessions/{session_id}/resume

Starts or resumes a session. Idempotent: calling resume on an already-running session returns `200` with the current row unchanged. Returns `409` when called on a terminal (`ended`) session.

**No request body required.**

**Response: 200** - `WorkspaceSession` with `status: "running"`. **Errors:** `404` not found, `409` session is ended.

---

## POST /v1/workspaces/{workspace_id}/sessions/{session_id}/pause

Requests a soft pause. The worker honours it at the next safe checkpoint.

**No request body required.**

**Response: 200** - Updated `WorkspaceSession`. **Errors:** `404` not found, `409` session is ended.

---

## POST /v1/workspaces/{workspace_id}/sessions/{session_id}/cancel

Hard-cancels a session. Transitions `CREATED` sessions immediately to `ended/cancelled`. Returns `409` on a session already in a terminal state.

**No request body required.**

**Response: 200** - `WorkspaceSession` with `status: "ended"` and `ended_reason: "cancelled"`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST "https://primer.example/v1/workspaces/ws-prod/sessions/sess-abc123/cancel" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/workspaces/ws-prod/sessions/sess-abc123/cancel",
    headers={"Authorization": f"Bearer {token}"},
)
r.raise_for_status()
session = r.json()
--- javascript
const r = await fetch(
  "/v1/workspaces/ws-prod/sessions/sess-abc123/cancel",
  { method: "POST", headers: { "Authorization": `Bearer ${token}` } },
);
const session = await r.json();
```

---

## POST /v1/workspaces/{workspace_id}/sessions/{session_id}/steer

Appends a steering instruction as a user-role message to the running session's transcript.

**Request body:** `{"instruction": "Focus on the authentication module"}`

**Response: 200** - The instruction object.

---

## GET /v1/sessions/{session_id}/ask_user/pending

Returns the current `ask_user` prompt when the session is parked waiting for operator input. Returns `404` when the session is not parked on `ask_user`.

```code-tabs:curl,python,javascript
--- curl
curl "https://primer.example/v1/sessions/sess-abc123/ask_user/pending" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/sessions/sess-abc123/ask_user/pending",
    headers={"Authorization": f"Bearer {token}"},
)
--- javascript
const r = await fetch("/v1/sessions/sess-abc123/ask_user/pending", {
  headers: { "Authorization": `Bearer ${token}` },
});
```

**Response: 200**

| Field | Type | Notes |
|-------|------|-------|
| `tool_call_id` | string | Required for the respond call |
| `prompt` | string | Text the agent sent to the operator |
| `parked_at` | string | ISO-8601 timestamp when the session parked |
| `response_schema` | object or null | Optional JSON Schema the response must conform to |

**Errors:** `404` no pending ask_user prompt.

---

## POST /v1/sessions/{session_id}/ask_user/respond

Submits the operator's reply to a pending `ask_user` prompt and resumes the session.

**Request body**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `tool_call_id` | string | yes | Must match the value from `/pending` |
| `response` | any | yes | String, object, array, number, or boolean; validated against `response_schema` when present |

```code-tabs:curl,python,javascript
--- curl
curl -X POST "https://primer.example/v1/sessions/sess-abc123/ask_user/respond" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool_call_id": "tc-001", "response": "Use the OAuth2 flow"}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/sessions/sess-abc123/ask_user/respond",
    headers={"Authorization": f"Bearer {token}"},
    json={"tool_call_id": "tc-001", "response": "Use the OAuth2 flow"},
)
r.raise_for_status()
--- javascript
const r = await fetch("/v1/sessions/sess-abc123/ask_user/respond", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ tool_call_id: "tc-001", response: "Use the OAuth2 flow" }),
});
```

**Response: 202** - `{"status": "accepted"}`. **Errors:** `404` no matching pending prompt.

---

## POST /v1/sessions/{session_id}/yields/{tool_call_id}/cancel

Cancels one in-flight yield by `tool_call_id`, causing the agent to receive a `YieldCancelled` result on its next turn.

**Request body:** `{"reason": "operator skipped"}` (optional; `reason` may be null or omitted)

**Response: 202** - `{"status": "accepted"}`. **Errors:** `404` session or yield not found.

---

## GET /v1/sessions/{session_id}/tool_approval/pending

Returns the pending tool approval request when the session is parked on the `_approval` tool. Returns `404` if the session is not parked on `_approval` (including when parked on a different tool such as `ask_user`).

```code-tabs:curl,python,javascript
--- curl
curl "https://primer.example/v1/sessions/sess-abc123/tool_approval/pending" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/sessions/sess-abc123/tool_approval/pending",
    headers={"Authorization": f"Bearer {token}"},
)
--- javascript
const r = await fetch("/v1/sessions/sess-abc123/tool_approval/pending", {
  headers: { "Authorization": `Bearer ${token}` },
});
```

**Response: 200**

| Field | Type | Notes |
|-------|------|-------|
| `tool_call_id` | string | Required for the respond call |
| `tool_name` | string | The inner tool name the agent tried to call |
| `arguments` | object | Arguments passed to the inner tool |
| `parked_at` | string | ISO-8601 timestamp |
| `timeout_at` | string or null | ISO-8601 expiry derived from `parked_at` + policy timeout |
| `policy_id` | string or null | Approval policy that triggered the gate |
| `approval_type` | string or null | e.g. `required` |
| `gate_reason` | string or null | Human-readable reason from the policy |

**Errors:** `404` not parked on `_approval`.

---

## POST /v1/sessions/{session_id}/tool_approval/respond

Submits an approval decision for the pending tool call and resumes the session.

**Request body**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `tool_call_id` | string | yes | Must match the value from `/pending` |
| `decision` | string | yes | `approved` or `rejected` |
| `reason` | string | no | Optional free-text reason (max 1024 chars) |

```code-tabs:curl,python,javascript
--- curl
curl -X POST "https://primer.example/v1/sessions/sess-abc123/tool_approval/respond" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool_call_id": "tc-shell-001", "decision": "approved", "reason": "operator confirmed"}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/sessions/sess-abc123/tool_approval/respond",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "tool_call_id": "tc-shell-001",
        "decision": "approved",
        "reason": "operator confirmed",
    },
)
r.raise_for_status()
--- javascript
const r = await fetch("/v1/sessions/sess-abc123/tool_approval/respond", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    tool_call_id: "tc-shell-001",
    decision: "approved",
    reason: "operator confirmed",
  }),
});
```

**Response: 202** - `{"status": "accepted"}`. **Errors:** `404` if `tool_call_id` does not match the parked yield (the parked id is never echoed in error responses).

---

## Error envelopes

All error responses use RFC 7807 problem details:

```code-tabs:curl,python,javascript
--- curl
# Example 404 response body
# {
#   "type": "/errors/not-found",
#   "title": "Not Found",
#   "status": 404,
#   "detail": "session not found"
# }
--- python
# Catch via r.raise_for_status() or inspect r.json()["type"]:
# "/errors/not-found"        : resource does not exist
# "/errors/conflict"         : illegal state transition (e.g. resume on ended)
# "/errors/validation-error" : request body failed schema validation
--- javascript
// On error, r.ok === false and r.json() returns:
// { type: "/errors/not-found", title: "...", status: 404, detail: "..." }
// Common types: /errors/not-found, /errors/conflict, /errors/validation-error
```
