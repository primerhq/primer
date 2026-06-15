---
slug: api-workers-health
title: Workers and Health API
section: reference
summary: Public endpoints to inspect worker pool state, in-flight capacity, and API liveness; plus worker drain.
---

The workers and health endpoints expose the runtime state of the worker pool and the scheduler. Both are public (no authentication required) and are suitable for use in load balancer health checks and operational dashboards.

```ref:workspaces/yielding-tools
How sessions yield control and park while waiting for events.
```

```ref:features/workers
Monitor the worker pool in the console.
```

## Endpoints

| Method | Path | Auth | Summary |
|--------|------|------|---------|
| GET | `/v1/workers` | none | List registered workers |
| POST | `/v1/workers/{worker_id}/drain` | required | Mark a worker as draining |
| GET | `/v1/health` | none | Liveness probe with scheduler and worker metrics |

## GET /v1/workers

Returns the list of workers registered in the pool with their current status, capacity, and heartbeat.

```json
{
  "items": [
    {
      "id": "wrk-d5ba94ecac66",
      "host": "build-host",
      "pid": 3371965,
      "capacity": 8,
      "started_at": "2026-06-07T19:00:55.238423Z",
      "last_heartbeat": "2026-06-07T19:00:55.238423Z",
      "status": "active"
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Worker identifier (prefix `wrk-`) |
| `host` | string | Hostname of the process |
| `pid` | integer | Operating system process id |
| `capacity` | integer | Maximum concurrent sessions this worker accepts |
| `started_at` | datetime | When the worker process registered |
| `last_heartbeat` | datetime | Timestamp of the most recent heartbeat |
| `status` | string | One of `active`, `draining`, `dead` |

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/workers
--- python
import httpx
r = httpx.get("https://your-host/v1/workers")
workers = r.json()["items"]
--- javascript
const r = await fetch("/v1/workers")
const {items} = await r.json()
```

**Note:** No `Authorization` header is needed. This endpoint is public.

## POST /v1/workers/{worker_id}/drain

Marks the specified worker as `draining`. The worker stops accepting new session claims but continues serving any sessions already in flight. Returns `204 No Content`.

The worker's `status` field on `GET /v1/workers` flips to `draining` immediately; the row remains visible throughout the drain window. The actual process shutdown happens externally (for example, via a SIGTERM to the worker process). Poll `GET /v1/workers` to observe the status transition.

```code-tabs:curl,python,javascript
--- curl
curl -X POST https://your-host/v1/workers/wrk-d5ba94ecac66/drain \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx, time
token = "..."
r = httpx.post(
    "https://your-host/v1/workers/wrk-d5ba94ecac66/drain",
    headers={"Authorization": f"Bearer {token}"},
)
assert r.status_code == 204

# Poll the drain status
for _ in range(30):
    workers = httpx.get("https://your-host/v1/workers").json()["items"]
    row = next((w for w in workers if w["id"] == "wrk-d5ba94ecac66"), None)
    if row and row["status"] == "draining":
        break
    time.sleep(0.5)
--- javascript
const r = await fetch("/v1/workers/wrk-d5ba94ecac66/drain", {
  method: "POST",
  headers: {"Authorization": `Bearer ${token}`}
})
// r.status === 204
```

**Guarantee:** Every `GET /v1/workers` and `GET /v1/health` call during the drain window returns a clean 2xx response with no `500` or `/errors/internal` envelopes. The drained worker row stays present and identifiable throughout.

**Errors:** `500` on internal failure.

## GET /v1/health

Liveness probe that returns `200 OK` whenever the API process is responsive. Includes scheduler liveness and worker pool capacity metrics.

```json
{
  "status": "ok",
  "version": "0.1.0",
  "scheduler": {
    "alive": true,
    "metrics": {
      "primer_sessions_active": {},
      "primer_sessions_runnable_queue_depth": 0,
      "primer_lease_expirations_total": 0,
      "primer_scheduler_notify_received_total": 0
    }
  },
  "worker_pool": {
    "in_flight": 0,
    "capacity": 8,
    "metrics": {
      "primer_worker_id": "wrk-d5ba94ecac66",
      "primer_worker_in_flight": 0,
      "primer_worker_capacity": 8,
      "primer_worker_claims_total": 1,
      "primer_worker_claims_empty_total": 7,
      "primer_session_turns_total": {},
      "primer_session_turn_duration_seconds": {"count": 0, "sum": 0.0}
    }
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `"ok"` when the process is responsive |
| `version` | string | API surface version (semver) |
| `scheduler.alive` | boolean | True when the in-process scheduler is running |
| `scheduler.metrics` | object | Snapshot of scheduler counters and gauges |
| `worker_pool.in_flight` | integer or null | Sessions currently executing; null in API-only mode |
| `worker_pool.capacity` | integer or null | Configured per-worker concurrency; null in API-only mode |
| `worker_pool.metrics` | object | Snapshot of worker-pool counters and histograms |

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/health
--- python
import httpx
r = httpx.get("https://your-host/v1/health")
health = r.json()
assert health["status"] == "ok"
capacity = health["worker_pool"]["capacity"]
in_flight = health["worker_pool"]["in_flight"]
--- javascript
const r = await fetch("/v1/health")
const health = await r.json()
const {capacity, in_flight} = health.worker_pool
```

**Note:** No `Authorization` header is needed. This endpoint is public and suitable for use as a load balancer health check target.

## Errors note

All error responses use the RFC 7807 `ProblemDetails` envelope with `type`, `title`, `status`, `detail`, `instance`, and `extensions` (which includes `request_id`). See the REST API overview for details.
