---
slug: env-vars
title: Environment variables
section: reference
summary: Every PRIMER_* env var; defaults; example values.
---

## Precedence

Env vars beat the TOML file beat the built-in defaults. Nested
config uses double underscore as the separator
(Pydantic's `env_nested_delimiter`).

```callout:info
Setting `PRIMER_DB__PROVIDER=postgres` is equivalent to setting
`db.provider = "postgres"` in the TOML. The double underscore is
load-bearing; a single underscore reads as part of the field
name.
```

## HTTP server

| Variable | Default | Example |
|---|---|---|
| `PRIMER_HOST` | `0.0.0.0` | `127.0.0.1` |
| `PRIMER_PORT` | `8000` | `9000` |

## Storage

| Variable | Default | Example |
|---|---|---|
| `PRIMER_DB__PROVIDER` | embedded sqlite | `postgres` |
| `PRIMER_DB__DSN` | (none; ~/.primer/db/data.sqlite) | `postgres://primer@db/primer` |
| `PRIMER_DB_SCHEMA` | unset | `primer_test_1` |

## Auth

| Variable | Default | Example |
|---|---|---|
| `PRIMER_AUTH__REQUIRE_AUTH` | `true` in prod | `false` (dev only) |
| `PRIMER_SESSION_SECRET` | auto-generated on first start | `$(openssl rand -hex 32)` |

## Worker pool

| Variable | Default | Example |
|---|---|---|
| `PRIMER_RUNTIME_MODE` | `api+worker` | `api`, `worker` |
| `PRIMER_WORKER__POOL_SIZE` | `8` | `16` |
| `PRIMER_WORKER__LEASE_TTL_SECONDS` | `60` | `120` |

## Workspace probe

| Variable | Default | Example |
|---|---|---|
| `PRIMER_WORKSPACE_PROBE_INTERVAL_SECONDS` | `30.0` | `10.0` |

## Observability

| Variable | Default | Example |
|---|---|---|
| `PRIMER_OBSERVABILITY__ENABLED` | `false` | `true` |
| `PRIMER_OBSERVABILITY__OTEL_ENDPOINT` | unset | `http://otel-collector:4318` |

## Documentation

| Variable | Default | Example |
|---|---|---|
| `PRIMER_USER_DOCS_STRICT` | unset | `1` (dev only) |

When set to `1`, doc lint errors raise RuntimeError from the
lifespan handler instead of being logged.

## Bootstrap

| Variable | Default | Example |
|---|---|---|
| `PRIMER_AUTO_BOOTSTRAP` | `true` | `false` (manual provisioning only) |

## MCP

| Variable | Default | Example |
|---|---|---|
| `PRIMER_MCP_STDIO_ALLOWED_COMMANDS` | unset (no allowlist) | `node,npx` |
| `PRIMER_MCP__RATE_LIMIT_PER_SECOND` | `10` | `30` |

## Config file path

| Variable | Default | Example |
|---|---|---|
| `PRIMER_CONFIG_PATH` | unset | `/etc/primer/primer.toml` |

When set, primer reads the TOML at this path during AppConfig
instantiation, before env vars override.
