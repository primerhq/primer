---
slug: env-vars
title: Environment variables
section: reference
summary: Every PRIMER_* env var, its default, and what it controls.
---

## Precedence

Env vars override the YAML/TOML config file which overrides built-in
defaults. Nested config fields use double underscore as the path
separator (pydantic-settings `env_nested_delimiter`).

```callout:info
`PRIMER_DB__PROVIDER=postgres` is equivalent to `db.provider: postgres`
in the YAML config. A single underscore is part of the field name; the
double underscore is the nesting delimiter.
```

## Config file path

| Variable | Default | Description |
|---|---|---|
| `PRIMER_CONFIG_PATH` | unset | Path to a TOML config file loaded during `AppConfig` instantiation, before env vars are applied. |

The CLI `--config` flag loads YAML through `init_settings` (higher
priority than env vars). `PRIMER_CONFIG_PATH` loads TOML at the env
layer (lower priority than CLI YAML, same layer as env vars).

## HTTP server

| Variable | Default | Description |
|---|---|---|
| `PRIMER_HOST` | `0.0.0.0` | Bind host for uvicorn. |
| `PRIMER_PORT` | `8000` | Bind port for uvicorn. |

## Storage

| Variable | Default | Description |
|---|---|---|
| `PRIMER_DB__PROVIDER` | unset (embedded SQLite) | Storage backend: `sqlite` or `postgres`. |
| `PRIMER_DB__CONFIG__PATH` | `~/.primer/db/data.sqlite` | SQLite database file path. Applies when provider is `sqlite`. |
| `PRIMER_DB__CONFIG__HOSTNAME` | (required for postgres) | Postgres host. |
| `PRIMER_DB__CONFIG__PORT` | `5432` | Postgres TCP port. |
| `PRIMER_DB__CONFIG__USERNAME` | (required for postgres) | Postgres role. |
| `PRIMER_DB__CONFIG__PASSWORD` | (required for postgres) | Postgres password. |
| `PRIMER_DB__CONFIG__DATABASE` | (required for postgres) | Postgres database name. |
| `PRIMER_DB__CONFIG__DB_SCHEMA` | `public` | Postgres schema for tables and indexes. |
| `PRIMER_DB_SCHEMA` | unset | Override the Postgres schema without restructuring the full `db.config` block. Intended for test isolation. No effect on SQLite. |

When `PRIMER_DB__PROVIDER` is unset, primer defaults to an embedded
SQLite database at `~/.primer/db/data.sqlite`.

## Runtime mode

| Variable | Default | Description |
|---|---|---|
| `PRIMER_RUNTIME_MODE` | `api+worker` | What this process does: `api`, `worker`, or `api+worker`. The CLI always sets this via `--no-worker` or the subcommand; set it here only when launching without the CLI. |

## Scheduler

| Variable | Default | Description |
|---|---|---|
| `PRIMER_SCHEDULER__PROVIDER` | `in_memory` (when mode includes worker) | Scheduler backend: `in_memory` or `postgres`. Use `postgres` for multi-process production deployments. |

## Worker pool

| Variable | Default | Description |
|---|---|---|
| `PRIMER_WORKER__CONCURRENCY` | `8` | Maximum simultaneous sessions the worker pool executes. |
| `PRIMER_WORKER__CLAIM_BATCH_SIZE` | `4` | Number of sessions claimed per poll iteration. |
| `PRIMER_WORKER__HEARTBEAT_INTERVAL_SECONDS` | `10` | How often a worker renews its session lease (seconds). |
| `PRIMER_WORKER__LEASE_TTL_SECONDS` | `30` | Seconds before an un-renewed lease expires. Must be at least 2x `heartbeat_interval_seconds`. |
| `PRIMER_WORKER__POLL_INTERVAL_SECONDS` | `2.0` | Seconds between claim-engine polling cycles. |
| `PRIMER_WORKER__DRAIN_TIMEOUT_SECONDS` | `120` | Seconds to wait for in-flight sessions to complete during graceful shutdown. |
| `PRIMER_WORKER__MAX_ATTEMPTS` | `5` | Maximum retries for a failing session before it is marked permanently failed. |
| `PRIMER_WORKER__BASE_BACKOFF_SECONDS` | `2.0` | Base exponential backoff interval between retries (seconds). |
| `PRIMER_WORKER__MAX_BACKOFF_SECONDS` | `300.0` | Maximum backoff cap between retries (seconds). |

## MCP stdio safety

| Variable | Default | Description |
|---|---|---|
| `PRIMER_MCP_STDIO_ALLOWED_COMMANDS` | unset (no allowlist) | Comma-separated list of executable names that MCP toolsets with transport `stdio` are allowed to launch. When unset, the check is disabled. |

## Workspace probe

| Variable | Default | Description |
|---|---|---|
| `PRIMER_WORKSPACE_PROBE_INTERVAL_SECONDS` | `30.0` | How often the workspace health-probe loop pings each running workspace (seconds). |

## Bootstrap

| Variable | Default | Description |
|---|---|---|
| `PRIMER_AUTO_BOOTSTRAP` | `true` | When `true`, primer runs first-boot auto-bootstrap on lifespan start if `bootstrap_completed_at` is null. Set `false` to skip and provision providers manually via the API or `primer init`. |

## Observability

| Variable | Default | Description |
|---|---|---|
| `PRIMER_OBSERVABILITY__ENABLED` | `true` | Master switch for OTEL tracing and Prometheus metrics. |
| `PRIMER_OBSERVABILITY__TRACES_ENABLED` | `true` | Enable OTEL trace export. Takes effect only when `enabled` is `true`. |
| `PRIMER_OBSERVABILITY__METRICS_ENABLED` | `true` | Enable Prometheus metrics. Takes effect only when `enabled` is `true`. |
| `PRIMER_OBSERVABILITY__TRACE_LLM_IO` | `false` | Include full LLM request/response payloads in trace spans. Generates large spans; off by default. |
| `PRIMER_OBSERVABILITY__OTLP_ENDPOINT` | unset | OTLP HTTP endpoint for trace export (e.g. `http://otel-collector:4318`). |
| `PRIMER_OBSERVABILITY__SERVICE_NAME` | `primer` | `service.name` resource attribute sent to the OTLP collector. |
| `PRIMER_OBSERVABILITY__SERVICE_NAMESPACE` | `default` | `service.namespace` resource attribute. |

## Auth

| Variable | Default | Description |
|---|---|---|
| `PRIMER_AUTH__ENABLED` | `true` | Enable the cookie-session auth middleware. Set `false` only for development. |
| `PRIMER_AUTH__SESSION_SECRET` | auto-generated | Cookie-signing secret. When set, takes priority over the auto-generated value stored in `system_state`. Rotate by changing this value. |
| `PRIMER_AUTH__SESSION_TTL_DAYS` | `7` | Session cookie lifetime in days. |
| `PRIMER_AUTH__COOKIE_SECURE` | `false` | Set `true` behind TLS to prevent the browser from sending the session cookie over plain HTTP. |
| `PRIMER_AUTH__COOKIE_SAMESITE` | `lax` | `SameSite` attribute on the session cookie. |

## Logging

| Variable | Default | Description |
|---|---|---|
| `PRIMER_LOG_LEVEL` | `debug` | Log level for the application and uvicorn access logs: `debug`, `info`, `warning`, or `error`. |
| `PRIMER_LOG_JSON` | `true` | When `true`, emit one JSON object per log line. When `false`, use a human-readable single-line formatter. |
| `PRIMER_LOG_FILE` | unset | Write logs to this file path (rotated). Unset keeps stdout/stderr behaviour. |

## Development flags

These variables are read directly by the application at runtime and
are not part of `AppConfig`.

| Variable | When set | Description |
|---|---|---|
| `PRIMER_USER_DOCS_STRICT` | `1` | Refuse to start when user-doc lint errors are found. Logs and continues by default. |
| `PRIMER_ENABLE_TEST_ENDPOINTS` | `1` | Mount internal instrumentation endpoints under `/v1/` used by the distributed test harness. Not for production use. |
