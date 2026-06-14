---
slug: api-triggers
title: Triggers and Subscriptions API
section: reference
summary: REST endpoints to create, fire, and manage triggers and subscriptions for event-driven execution.
---

Triggers are named event sources (one-shot delayed or recurring cron) that fire subscriptions, which in turn post chat messages, start fresh agent or graph sessions, or resume parked sessions. A running agent can also park itself on a trigger via `subscribe_to_trigger` and resume automatically when the trigger fires.

```ref:features/triggers
The park/resume model, subscription kinds, and webhook setup.
```

```ref:features/triggers
Create and manage triggers in the console.
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/v1/triggers` | List triggers |
| POST | `/v1/triggers` | Create a trigger |
| GET | `/v1/triggers/{trigger_id}` | Get a trigger |
| PUT | `/v1/triggers/{trigger_id}` | Replace a trigger |
| DELETE | `/v1/triggers/{trigger_id}` | Delete a trigger (cascades to subscriptions) |
| POST | `/v1/triggers/{trigger_id}/fire_now` | Synchronously fire a trigger |
| GET | `/v1/triggers/{trigger_id}/subscriptions` | List subscriptions for a trigger |
| POST | `/v1/triggers/{trigger_id}/subscriptions` | Create a subscription |
| GET | `/v1/triggers/{trigger_id}/subscriptions/{subscription_id}` | Get a subscription |
| PUT | `/v1/triggers/{trigger_id}/subscriptions/{subscription_id}` | Replace a subscription |
| DELETE | `/v1/triggers/{trigger_id}/subscriptions/{subscription_id}` | Delete a subscription |

## Trigger object

```json
{
  "id": "daily-report",
  "slug": "daily-report",
  "name": "Daily Report",
  "description": "Fires every morning at 08:00 UTC.",
  "config": {
    "kind": "scheduled",
    "cron": "0 8 * * *",
    "timezone": "UTC",
    "catchup": "one"
  },
  "enabled": true
}
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `slug` | yes | string | URL-safe identifier (2-64 chars) |
| `name` | yes | string | Human-readable label (1-200 chars) |
| `description` | no | string | Optional description (max 2000 chars) |
| `config` | yes | TriggerConfig | Either a `delayed` or `scheduled` config (see below) |
| `enabled` | no | boolean | Default `true`; when `false` the trigger never fires |

**Delayed trigger config** (`kind: "delayed"`):

| Field | Required | Description |
|-------|----------|-------------|
| `kind` | yes | Must be `"delayed"` |
| `fire_at` | yes | ISO 8601 datetime (UTC); the trigger fires once at this time |

**Scheduled trigger config** (`kind: "scheduled"`):

| Field | Required | Description |
|-------|----------|-------------|
| `kind` | yes | Must be `"scheduled"` |
| `cron` | yes | Standard 5-field cron expression |
| `timezone` | no | IANA timezone name; default `"UTC"` |
| `catchup` | no | `"one"` (default), `"all"`, or `"none"` -- controls missed-fire catch-up behavior |

## Create a trigger

`POST /v1/triggers` - returns `201 Created` with the trigger object.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/triggers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "daily-report",
    "name": "Daily Report",
    "config": {
      "kind": "scheduled",
      "cron": "0 8 * * *",
      "timezone": "UTC",
      "catchup": "one"
    },
    "enabled": true
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/triggers",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "slug": "daily-report",
        "name": "Daily Report",
        "config": {
            "kind": "scheduled",
            "cron": "0 8 * * *",
            "timezone": "UTC",
            "catchup": "one",
        },
        "enabled": True,
    },
)
assert r.status_code == 201
--- javascript
const r = await fetch("/v1/triggers", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    slug: "daily-report",
    name: "Daily Report",
    config: {kind: "scheduled", cron: "0 8 * * *", timezone: "UTC", catchup: "one"},
    enabled: true
  })
})
```

**Errors:**
- `422` - validation failed (missing required field, invalid cron, bad `fire_at` format)
- `409` - a trigger with this `slug` already exists

## Get a trigger

`GET /v1/triggers/{trigger_id}` - returns `200 OK` with the trigger object.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/triggers/daily-report \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/triggers/daily-report",
              headers={"Authorization": f"Bearer {token}"})
--- javascript
const r = await fetch("/v1/triggers/daily-report", {
  headers: {"Authorization": `Bearer ${token}`}
})
```

**Errors:** `404` if the trigger does not exist.

## List triggers

`GET /v1/triggers` - returns an offset or cursor page of trigger objects.

Query parameters: `limit` (1-200, default 20), `offset` (default 0), `cursor`, `order_by`.

```code-tabs:curl,python,javascript
--- curl
curl "https://your-host/v1/triggers?limit=20&offset=0" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get("https://your-host/v1/triggers",
              headers={"Authorization": f"Bearer {token}"},
              params={"limit": 20, "offset": 0})
page = r.json()
triggers = page["items"]
--- javascript
const r = await fetch("/v1/triggers?limit=20&offset=0", {
  headers: {"Authorization": `Bearer ${token}`}
})
const {items, total} = await r.json()
```

## Replace a trigger

`PUT /v1/triggers/{trigger_id}` - full replacement; returns `200 OK`. All fields are replaced; omitted optional fields reset to defaults.

```code-tabs:curl,python,javascript
--- curl
curl -X PUT https://your-host/v1/triggers/daily-report \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "daily-report",
    "name": "Daily Report (updated)",
    "config": {"kind": "scheduled", "cron": "0 9 * * *", "timezone": "UTC"},
    "enabled": true
  }'
--- python
import httpx
r = httpx.put(
    "https://your-host/v1/triggers/daily-report",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "slug": "daily-report",
        "name": "Daily Report (updated)",
        "config": {"kind": "scheduled", "cron": "0 9 * * *", "timezone": "UTC"},
        "enabled": True,
    },
)
--- javascript
await fetch("/v1/triggers/daily-report", {
  method: "PUT",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    slug: "daily-report",
    name: "Daily Report (updated)",
    config: {kind: "scheduled", cron: "0 9 * * *", timezone: "UTC"},
    enabled: true
  })
})
```

**Errors:** `404` if not found.

## Delete a trigger

`DELETE /v1/triggers/{trigger_id}` - cascades to all subscriptions. Returns `200 OK`.

```code-tabs:curl,python,javascript
--- curl
curl -X DELETE https://your-host/v1/triggers/daily-report \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete("https://your-host/v1/triggers/daily-report",
                 headers={"Authorization": f"Bearer {token}"})
--- javascript
await fetch("/v1/triggers/daily-report", {
  method: "DELETE",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Fire a trigger now

`POST /v1/triggers/{trigger_id}/fire_now` - synchronously executes all enabled subscriptions for this trigger regardless of schedule. Returns `200 OK`.

Response fields: `skipped` (boolean), `results` (array of per-subscription outcomes with `ok`, `skipped`, `error_code`, and `artefact_id`).

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/triggers/daily-report/fire_now \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/triggers/daily-report/fire_now",
    headers={"Authorization": f"Bearer {token}"},
)
fire = r.json()
print(fire["results"])
--- javascript
const r = await fetch("/v1/triggers/daily-report/fire_now", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`}
})
const {skipped, results} = await r.json()
```

**Errors:** `404` if the trigger does not exist.

## Subscription object

```json
{
  "id": "sub-001",
  "config": {
    "kind": "agent_fresh_session",
    "workspace_id": "ws-prod",
    "agent_id": "daily-report-agent"
  },
  "enabled": true,
  "parallelism": "skip",
  "payload_template": null
}
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `config` | yes | SubConfig | Discriminated subscription config (see kinds below) |
| `description` | no | string | Optional description (max 2000 chars) |
| `enabled` | no | boolean | Default `true`; disabled subscriptions are skipped on fire |
| `parallelism` | no | string | `"skip"` (default) -- behavior when a prior fire's action is still running |
| `payload_template` | no | string or null | Jinja-style template rendered into the action payload |

**Subscription kinds:**

`agent_fresh_session` -- start a new agent session when the trigger fires:

| Field | Required | Description |
|-------|----------|-------------|
| `kind` | yes | `"agent_fresh_session"` |
| `workspace_id` | yes | Id of the workspace to run the session in |
| `agent_id` | yes | Id of the agent to run |

`graph_fresh_session` -- start a new graph run:

| Field | Required | Description |
|-------|----------|-------------|
| `kind` | yes | `"graph_fresh_session"` |
| `workspace_id` | yes | Id of the workspace |
| `graph_id` | yes | Id of the graph |

`chat_message` -- post a message into a chat:

| Field | Required | Description |
|-------|----------|-------------|
| `kind` | yes | `"chat_message"` |
| `chat_id` | yes | Id of the target chat |

`parked_session` -- resume a session parked on `subscribe_to_trigger`. Created automatically by the engine when an agent calls the `trigger__subscribe_to_trigger` yielding tool; rarely created directly via REST:

| Field | Required | Description |
|-------|----------|-------------|
| `kind` | yes | `"parked_session"` |
| `session_id` | yes | Id of the parked session |
| `tool_call_id` | yes | Tool call id of the `subscribe_to_trigger` invocation |
| `parked_at` | yes | ISO 8601 timestamp when the session parked |

## Create a subscription

`POST /v1/triggers/{trigger_id}/subscriptions` - returns `201 Created`.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/triggers/daily-report/subscriptions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "kind": "agent_fresh_session",
      "workspace_id": "ws-prod",
      "agent_id": "daily-report-agent"
    },
    "enabled": true,
    "parallelism": "skip"
  }'
--- python
import httpx
r = httpx.post(
    "https://your-host/v1/triggers/daily-report/subscriptions",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "config": {
            "kind": "agent_fresh_session",
            "workspace_id": "ws-prod",
            "agent_id": "daily-report-agent",
        },
        "enabled": True,
        "parallelism": "skip",
    },
)
assert r.status_code == 201
--- javascript
const r = await fetch("/v1/triggers/daily-report/subscriptions", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({
    config: {
      kind: "agent_fresh_session",
      workspace_id: "ws-prod",
      agent_id: "daily-report-agent"
    },
    enabled: true,
    parallelism: "skip"
  })
})
```

**Errors:** `404` if the trigger does not exist; `422` if the config is invalid.

## List subscriptions

`GET /v1/triggers/{trigger_id}/subscriptions` - returns a page of subscription objects. One-shot `parked_session` subscriptions are removed automatically once consumed.

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/triggers/daily-report/subscriptions \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://your-host/v1/triggers/daily-report/subscriptions",
    headers={"Authorization": f"Bearer {token}"},
)
subs = r.json()["items"]
--- javascript
const r = await fetch("/v1/triggers/daily-report/subscriptions", {
  headers: {"Authorization": `Bearer ${token}`}
})
const {items} = await r.json()
```

## Delete a subscription

`DELETE /v1/triggers/{trigger_id}/subscriptions/{subscription_id}` - returns `200 OK`.

```code-tabs:curl,python,javascript
--- curl
curl -X DELETE https://your-host/v1/triggers/daily-report/subscriptions/sub-001 \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete(
    "https://your-host/v1/triggers/daily-report/subscriptions/sub-001",
    headers={"Authorization": f"Bearer {token}"},
)
--- javascript
await fetch("/v1/triggers/daily-report/subscriptions/sub-001", {
  method: "DELETE",
  headers: {"Authorization": `Bearer ${token}`}
})
```

## Errors note

All error responses use the RFC 7807 `ProblemDetails` envelope with `type`, `title`, `status`, `detail`, `instance`, and `extensions` (which includes `request_id` and, for 422 errors, an `errors` array with field paths). See the REST API overview for details.
