---
slug: observability
title: Observability
section: features
summary: "Instrument and observe a running platform: the liveness health probe, the Prometheus metrics endpoint, worker inspection and drain, and structured logging."
---

## What you can observe

A running Primer process exposes its runtime state through three always-available surfaces and one configurable one:

- **`GET /v1/health`**: a liveness probe that folds in a snapshot of scheduler and worker-pool metrics. Public (no auth).
- **`GET /v1/workers`**: the list of registered workers, their capacity, and their last heartbeat. Public (no auth). `POST /v1/workers/{id}/drain` takes a worker out of rotation cleanly.
- **`GET /metrics`**: a Prometheus exposition endpoint backed by a dedicated registry. Enabled by default; can be turned off.
- **Structured logs**: one JSON object per line by default, suitable for a log aggregator.

The health and metrics surfaces overlap on purpose: `/v1/health` lets a dashboard scrape scheduler and pool counters without a Prometheus collector, while `/metrics` is the full counter and histogram set for a real scrape pipeline.

## Liveness: GET /v1/health

`GET /v1/health` returns `200 OK` whenever the process is responsive. Alongside the constant `status` and the API `version`, it surfaces a light-touch snapshot of scheduler liveness and worker-pool capacity so a monitor can read pool pressure without a second endpoint.

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

- `scheduler.alive` is `true` when the process has a live scheduler instance. `scheduler.metrics` carries the scheduler snapshot (see below). It is empty when the scheduler is absent.
- `worker_pool.in_flight` and `worker_pool.capacity` are `null` in an API-only process (no attached pool). `worker_pool.metrics` carries the full pool snapshot.

A broken metrics snapshot never takes the endpoint down: a failing snapshot degrades to an empty object so the probe still returns `200`.

The scheduler snapshot reports:

| Key | Meaning |
|---|---|
| `primer_sessions_active` | Count of sessions by status (a map keyed by status value). |
| `primer_sessions_runnable_queue_depth` | Runnable leases not yet claimed by any worker. |
| `primer_lease_expirations_total` | Leases that expired (worker stalled or crashed without releasing). |
| `primer_scheduler_notify_received_total` | Wake-up notifications the scheduler received. |

This is the snapshot the default in-process (in-memory) scheduler returns. A Postgres-backed scheduler reports a reduced synchronous snapshot under `/v1/health` (`primer_scheduler_notify_received_total` plus `primer_scheduler_listen_reconnects_total`); its session, queue-depth, and lease-expiration gauges are computed in a separate database query that the health probe does not call. Scrape `/metrics` for the full counter set under Postgres.

The worker-pool snapshot reports:

| Key | Meaning |
|---|---|
| `primer_worker_id` | This pool's worker identifier (prefix `wrk-`). |
| `primer_worker_in_flight` | Sessions currently executing in this pool. |
| `primer_worker_capacity` | Configured per-worker concurrency. |
| `primer_worker_claims_total` | Claim attempts that won a runnable lease. |
| `primer_worker_claims_empty_total` | Poll cycles that found nothing to claim. |
| `primer_session_turns_total` | Completed turns by result (a map keyed by result). |
| `primer_session_turn_duration_seconds` | Turn duration as `{count, sum}` (a real exporter folds this into buckets). |

```code-tabs:curl,python,javascript
--- curl
curl https://your-host/v1/health
--- python
import httpx
health = httpx.get("https://your-host/v1/health").json()
assert health["status"] == "ok"
capacity = health["worker_pool"]["capacity"]
in_flight = health["worker_pool"]["in_flight"]
--- javascript
const r = await fetch("/v1/health")
const health = await r.json()
const {capacity, in_flight} = health.worker_pool
```

## Workers: inspect and drain

`GET /v1/workers` lists every worker registered with the scheduler, each with its host, pid, capacity, last heartbeat, and status (`active`, `draining`, or `dead`). It is public so a load balancer can poll it pre-login. Drain a worker before a planned restart so its in-flight sessions finish cleanly instead of being re-claimed mid-turn: `POST /v1/workers/{worker_id}/drain` flips the worker to `draining` (this call requires auth) and the worker stops accepting new claims while it finishes what it holds.

The console **Workers** page renders this surface live, with a summary strip (total / active / running-now / scheduler) and a per-worker table.

```ref:features/workers
The worker pool, claim and lease model, park and resume, and the Workers page.
```

```ref:reference/api-workers-health
Full request and response shapes for the workers list, drain, and health endpoints.
```

## Metrics: GET /metrics

When observability is enabled, the process mounts a Prometheus exposition endpoint at `GET /metrics`. It is backed by a dedicated registry, so the endpoint returns only Primer-defined metrics, not the default process or platform collectors. When `PRIMER_OBSERVABILITY__ENABLED` or `PRIMER_OBSERVABILITY__METRICS_ENABLED` is `false`, the mount is skipped and `GET /metrics` returns `404`.

The registry defines these metrics:

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `llm_tokens_total` | counter | `provider`, `direction` | LLM tokens processed, by provider and in/out direction. |
| `llm_duration_seconds` | histogram | `provider` | LLM streaming call duration. |
| `llm_failure_total` | counter | `provider`, `error_type` | LLM call failures, by provider and error type. |
| `llm_retry_total` | counter | `provider` | LLM call retries, by provider. |
| `tool_calls_total` | counter | `name`, `outcome` | Tool calls, by tool name and ok/fail outcome. |
| `tool_duration_seconds` | histogram | `name` | Tool execution duration, by tool name. |
| `claim_enqueue_latency_seconds` | histogram | `kind` | Time a lease waited in the queue before being claimed. |
| `claim_queue_depth` | gauge | `kind` | Unclaimed leases currently in the queue. |
| `claim_active_count` | gauge | `kind` | Active (claimed, not yet completed) leases. |
| `ws_connections_active` | gauge | `kind` | Active WebSocket connections. |
| `ws_frames_sent_total` | counter | `kind` | WebSocket frames sent. |
| `ws_session_duration_seconds` | histogram | `kind` | WebSocket session duration. |
| `ws_replay_backlog_seconds` | histogram | `kind` | Age of the oldest replayed event at WS connect time. |

The `primer_*` counters surfaced under `/v1/health` (claims, turns, lease expirations, notifications) are the scheduler and pool snapshot fields described above; the table here is the registry scraped by a Prometheus pipeline.

```callout:info
The metrics endpoint is mounted at the host root (`/metrics`), not under the `/v1` API prefix, so a scrape config targets `https://your-host/metrics` directly.
```

```code-tabs:curl,python
--- curl
curl https://your-host/metrics
--- python
import httpx
text = httpx.get("https://your-host/metrics").text
# Prometheus text exposition format; feed to your scrape pipeline.
```

## Tracing

When tracing is enabled and an OTLP endpoint is configured, the process exports OpenTelemetry spans to the collector. Tracing shares the observability master switch and has its own toggle and endpoint. By default the LLM request and response payloads are not included in spans (they produce large spans); a dedicated flag turns that on when you need it.

| Setting | Default | Purpose |
|---|---|---|
| `PRIMER_OBSERVABILITY__ENABLED` | `true` | Master switch for tracing and metrics. |
| `PRIMER_OBSERVABILITY__TRACES_ENABLED` | `true` | Export OTEL traces (effective only when enabled). |
| `PRIMER_OBSERVABILITY__METRICS_ENABLED` | `true` | Serve `/metrics` (effective only when enabled). |
| `PRIMER_OBSERVABILITY__TRACE_LLM_IO` | `false` | Include full LLM request/response payloads in spans. |
| `PRIMER_OBSERVABILITY__OTLP_ENDPOINT` | unset | OTLP HTTP endpoint for trace export. |
| `PRIMER_OBSERVABILITY__SERVICE_NAME` | `primer` | `service.name` resource attribute. |
| `PRIMER_OBSERVABILITY__SERVICE_NAMESPACE` | `default` | `service.namespace` resource attribute. |

```ref:reference/env-vars
Every observability environment variable and its default.
```

## Structured logging

Logging is configured once at startup. The default format is JSON: one self-contained object per line carrying `timestamp` (ISO 8601 UTC), `level`, `logger`, and `message`, plus any structured fields attached at the call site. Stack traces from error logging land under a `traceback` key. This format is safe to ship straight to a log aggregator.

A human-readable single-line dev format is available for local hacking: `<timestamp> [<level>] <logger>: <message>` with stack traces inline.

Because the root logger is configured, every logger in Primer code and in its dependencies (the LLM SDKs, httpx, and so on) inherits the same format; you can silence or re-route individual logger names afterward through the standard library.

## What to watch

A practical baseline:

- **Liveness**: poll `GET /v1/health` and alert when it stops returning `200` or when `scheduler.alive` goes false.
- **Pool saturation**: watch `worker_pool.in_flight` against `worker_pool.capacity`. Sustained saturation means work is queuing; add worker capacity.
- **Queue depth**: a rising `primer_sessions_runnable_queue_depth` (health) or `claim_queue_depth` (metrics) with no free slots is the same saturation signal from the scheduler side.
- **Lease expirations**: a climbing `primer_lease_expirations_total` points at workers stalling or crashing mid-turn.
- **LLM failures and retries**: `llm_failure_total` and `llm_retry_total` by provider surface a failing or rate-limited upstream.
```
