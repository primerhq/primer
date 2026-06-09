---
slug: api-tool-approval
title: Tool Approval API
section: reference
summary: REST endpoints to configure approval policies and submit operator decisions for gated tool calls.
---

Tool approval policies define gates that intercept tool calls before execution. Three strategy types are supported: `required` (always blocks for a human decision), `policy` (evaluates a Rego policy against the call context), and `llm` (delegates to an LLM judge). When a gate trips, the session parks on `_approval` and a decision must be submitted via the respond endpoint before execution continues.

```ref:concepts/tool-approval
The approval gate lifecycle and park/resume model.
```

```ref:features/tool-approval
Configure approval policies in the console.
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/tool_approval_policies` | List policies |
| POST | `/v1/tool_approval_policies` | Create a policy |
| POST | `/v1/tool_approval_policies/find` | Find policies by predicate |
| POST | `/v1/tool_approval_policies/invalidate` | Flush the in-process policy cache |
| GET | `/v1/tool_approval_policies/{entity_id}` | Get a policy |
| PUT | `/v1/tool_approval_policies/{entity_id}` | Replace a policy |
| DELETE | `/v1/tool_approval_policies/{entity_id}` | Delete a policy |
| GET | `/v1/sessions/{session_id}/tool_approval/pending` | Get pending approval for a session |
| POST | `/v1/sessions/{session_id}/tool_approval/respond` | Submit an approval decision for a session |
| GET | `/v1/chats/{chat_id}/tool_approval/pending` | Get pending approval for a chat |
| POST | `/v1/chats/{chat_id}/tool_approval/respond` | Submit an approval decision for a chat |

## ToolApprovalPolicy object

```json
{
  "id": "gate-delete-files",
  "toolset_id": "_workspaces",
  "tool_name": "fs_delete",
  "approval": {"type": "required"},
  "enabled": true,
  "timeout_seconds": 300
}
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `id` | no | string | Identifier. If omitted, the server assigns a type-prefixed id (e.g. `tool-approval-policy-3f9a1c8d`). Immutable after creation |
| `toolset_id` | yes | string | Toolset the policy applies to; may be a reserved internal toolset (`_system`, `_workspaces`, `_misc`, `_search`, `web`) or a user-created Toolset id |
| `tool_name` | yes | string | Bare tool name as registered in the provider catalogue (min length 1) |
| `approval` | yes | ApprovalConfig | Discriminated approval strategy (see below) |
| `enabled` | no | boolean | Default `true`; disabled policies are stored but skipped at evaluation time |
| `timeout_seconds` | no | number or null | Per-policy timeout in seconds (must be > 0). `null` falls back to the global yield cap |

The pair `(toolset_id, tool_name)` must be unique. A duplicate returns `409 /errors/conflict` naming both fields. This constraint also applies on PUT: the check skips the row being updated so a policy can be replaced in place without a false duplicate.

**Approval strategies:**

`required` -- gate trips unconditionally; every call waits for an operator decision:

```json
{"type": "required"}
```

`policy` -- evaluate a Rego policy source. The policy must declare `package primer.tool_approval` and expose a boolean `required` key (and an optional `reason` string). The source is compile-tested server-side at create/update time; malformed Rego returns `422` and never persists:

```json
{
  "type": "policy",
  "policy": "package primer.tool_approval\n\ndefault required := false\n\nrequired {\n    input.arguments.amount > 10000\n}\n"
}
```

`llm` -- delegate to an LLM judge. The `provider_id` must reference an existing LLMProvider row and `model` must be in that provider's `models` list; both are validated server-side at create/update time:

```json
{
  "type": "llm",
  "provider_id": "anthropic-prod",
  "model": "claude-sonnet-4-6",
  "prompt": "Decide whether this tool call should be approved. Approve only if the operation is safe and expected."
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `provider_id` | yes | Id of an existing LLMProvider |
| `model` | yes | Model name from that provider's `models` list |
| `prompt` | yes | System prompt sent to the judge (1-16000 chars); call context is appended as the user message |

## Create a policy

`POST /v1/tool_approval_policies` - returns `201 Created`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/tool_approval_policies \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "gate-delete-files",
    "toolset_id": "_workspaces",
    "tool_name": "fs_delete",
    "approval": {"type": "required"},
    "enabled": true,
    "timeout_seconds": 300
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/tool_approval_policies",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "gate-delete-files",
        "toolset_id": "_workspaces",
        "tool_name": "fs_delete",
        "approval": {"type": "required"},
        "enabled": True,
        "timeout_seconds": 300,
    },
)
assert r.status_code == 201
--- javascript
const r = await fetch("/v1/tool_approval_policies", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "gate-delete-files",
    toolset_id: "_workspaces",
    tool_name: "fs_delete",
    approval: {type: "required"},
    enabled: true,
    timeout_seconds: 300
  })
})
```

**Errors:**
- `409` - `(toolset_id, tool_name)` pair already has a policy
- `422` - required field missing, Rego compile failed, or LLM provider/model not found

## Get a policy

`GET /v1/tool_approval_policies/{entity_id}` - returns `200 OK`.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/tool_approval_policies/gate-delete-files \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/tool_approval_policies/gate-delete-files",
              headers={"Authorization": f"Bearer {token}"})
pol = r.json()
--- javascript
const r = await fetch("/v1/tool_approval_policies/gate-delete-files", {
  headers: {"Authorization": `Bearer ${token}`}
})
```

**Errors:** `404` if the id does not exist.

## List policies

`GET /v1/tool_approval_policies` - returns an offset or cursor page of policy objects.

Query parameters: `limit` (1-200, default 20), `offset` (default 0), `cursor`, `order_by`.

```code-tabs:curl,python,javascript
--- curl
curl "https://your-host/v1/tool_approval_policies?limit=20" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/tool_approval_policies",
              headers={"Authorization": f"Bearer {token}"})
policies = r.json()["items"]
--- javascript
const r = await fetch("/v1/tool_approval_policies?limit=20", {
  headers: {"Authorization": `Bearer ${token}`}
})
const {items} = await r.json()
```

## Replace a policy

`PUT /v1/tool_approval_policies/{entity_id}` - full replacement; returns `200 OK`. The same validation rules apply as on POST (Rego compile-test, LLM provider lookup, uniqueness skips own id).

```code-tabs:curl,python,javascript
--- curl
curl -X PUT https://your-host/v1/tool_approval_policies/gate-delete-files \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "gate-delete-files",
    "toolset_id": "_workspaces",
    "tool_name": "fs_delete",
    "approval": {"type": "required"},
    "enabled": false,
    "timeout_seconds": 120
  }'
--- python
import httpx
r = httpx.put(
    "https://your-host/v1/tool_approval_policies/gate-delete-files",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "gate-delete-files",
        "toolset_id": "_workspaces",
        "tool_name": "fs_delete",
        "approval": {"type": "required"},
        "enabled": False,
        "timeout_seconds": 120,
    },
)
--- javascript
await fetch("/v1/tool_approval_policies/gate-delete-files", {
  method: "PUT",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    id: "gate-delete-files",
    toolset_id: "_workspaces",
    tool_name: "fs_delete",
    approval: {type: "required"},
    enabled: false,
    timeout_seconds: 120
  })
})
```

## Delete a policy

`DELETE /v1/tool_approval_policies/{entity_id}` - returns `200 OK`.

```code-tabs:curl,python,javascript
--- curl
curl -X DELETE https://your-host/v1/tool_approval_policies/gate-delete-files \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete(
    "https://your-host/v1/tool_approval_policies/gate-delete-files",
    headers={"Authorization": f"Bearer {token}"},
)
--- javascript
await fetch("/v1/tool_approval_policies/gate-delete-files", {
  method: "DELETE",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Invalidate the policy cache

`POST /v1/tool_approval_policies/invalidate` - flushes the in-process approval policy cache so the running engine picks up newly created or deleted policies without waiting for the next cache refresh. Returns `202 Accepted`.

Call this after creating or deleting a policy if the change needs to take effect immediately for in-flight sessions.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/tool_approval_policies/invalidate \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/tool_approval_policies/invalidate",
    headers={"Authorization": f"Bearer {token}"},
)
assert r.status_code == 202
--- javascript
await fetch("/v1/tool_approval_policies/invalidate", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Get pending approval

When an enabled policy gates a tool call, the session parks on `_approval`. The pending endpoint returns the queued call details.

`GET /v1/sessions/{session_id}/tool_approval/pending` - returns `200 OK` with the pending call, or `204 No Content` if there is no pending approval.

`GET /v1/chats/{chat_id}/tool_approval/pending` - same for a chat context.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/sessions/sess-abc123/tool_approval/pending \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://your-host/v1/sessions/sess-abc123/tool_approval/pending",
    headers={"Authorization": f"Bearer {token}"},
)
pending = r.json()
tool_call_id = pending["tool_call_id"]
--- javascript
const r = await fetch("/v1/sessions/sess-abc123/tool_approval/pending", {
  headers: {"Authorization": `Bearer ${token}`}
})
const pending = await r.json()
```

## Submit an approval decision

`POST /v1/sessions/{session_id}/tool_approval/respond` - submit an `approved` or `rejected` decision. The session resumes with the result.

`POST /v1/chats/{chat_id}/tool_approval/respond` - same for a chat context.

Request body:

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `tool_call_id` | yes | string | The tool call id from the pending response |
| `decision` | yes | string | `"approved"` or `"rejected"` |
| `reason` | no | string or null | Optional human-readable reason (max 1024 chars); surfaced to the agent in the tool result on rejection |

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/sessions/sess-abc123/tool_approval/respond \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tool_call_id": "tc-xyz789",
    "decision": "approved"
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/sessions/sess-abc123/tool_approval/respond",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "tool_call_id": "tc-xyz789",
        "decision": "approved",
    },
)
--- javascript
await fetch("/v1/sessions/sess-abc123/tool_approval/respond", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    tool_call_id: "tc-xyz789",
    decision: "approved"
  })
})
```

To reject with a reason:

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/sessions/sess-abc123/tool_approval/respond \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tool_call_id": "tc-xyz789",
    "decision": "rejected",
    "reason": "File deletion not permitted during business hours."
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/sessions/sess-abc123/tool_approval/respond",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "tool_call_id": "tc-xyz789",
        "decision": "rejected",
        "reason": "File deletion not permitted during business hours.",
    },
)
--- javascript
await fetch("/v1/sessions/sess-abc123/tool_approval/respond", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    tool_call_id: "tc-xyz789",
    decision: "rejected",
    reason: "File deletion not permitted during business hours."
  })
})
```

**Errors:** `404` if the session or chat does not exist; `422` if `tool_call_id` is missing or `decision` is not a valid value.

**Timeout behavior:** if no decision arrives before `timeout_seconds` elapses, the TimeoutSweeper synthesises a rejected result (reason `"timed-out"`) and the session resumes with that rejection. The session does not terminate; the agent receives the rejection and continues.

## Errors note

All error responses use the RFC 7807 `ProblemDetails` envelope with `type`, `title`, `status`, `detail`, `instance`, and `extensions` (which includes `request_id` and, for 422 errors, an `errors` array with field paths). See the REST API overview for details.
