---
slug: configuration
title: Configuration
section: getting-started
summary: The config file, environment variables, and the order primer reads them in.
---

## Three sources, one order

Primer reads configuration from three places, merged in order of
precedence from lowest to highest:

1. Defaults baked into the `AppConfig` Pydantic settings class.
2. A YAML file supplied via `--config` (or `~/.primer/config.yaml`
   when it exists). CLI-supplied YAML wins over environment variables.
3. A TOML file at `$PRIMER_CONFIG_PATH` (same precedence layer as
   env vars).
4. Environment variables prefixed with `PRIMER_`.

Later sources win. An env var beats a TOML setting; a CLI `--config`
YAML beats env vars; built-in defaults lose to everything.

```callout:warning
Secrets follow the same precedence rule. If you commit a config file
containing `PRIMER_AUTH__SESSION_SECRET`, anyone who can read the
file has the secret even if a production env var overrides it at
runtime. Keep secrets in env vars or in a secrets manager, not in
committed config files.
```

## The config file

`primer api` auto-discovers `~/.primer/config.yaml` when no explicit
`--config` flag is given. Point to a different file explicitly:

```code-tabs:bash
--- bash
uv run primer api --config /etc/primer/config.yaml
```

A minimal production config covers storage, auth, and the bind
address. Everything else has a sensible default:

```code-tabs:yaml
--- yaml
# /etc/primer/config.yaml
host: "0.0.0.0"
port: 8000

db:
  provider: postgres
  config:
    hostname: db.example
    port: 5432
    username: primer
    password: secret
    database: primer

auth:
  enabled: true
  cookie_secure: true
```

## Environment variables

Every `AppConfig` field is reachable via environment variable using
the `PRIMER_` prefix. Nested fields use double-underscore as the path
separator (pydantic-settings `env_nested_delimiter`):

```code-tabs:bash
--- bash
# Top-level scalar
export PRIMER_PORT=9000

# Nested: db.provider
export PRIMER_DB__PROVIDER=postgres

# Nested: auth.enabled
export PRIMER_AUTH__ENABLED=true
```

A single underscore is part of a field name; the double underscore is
the nesting delimiter.

## Key settings at a glance

| Area | Key variable | Default |
|---|---|---|
| Storage | `PRIMER_DB__PROVIDER` | embedded SQLite |
| Runtime mode | `PRIMER_RUNTIME_MODE` | `api+worker` |
| Bind host | `PRIMER_HOST` | `0.0.0.0` |
| Bind port | `PRIMER_PORT` | `8000` |
| Auth | `PRIMER_AUTH__ENABLED` | `true` |
| Session secret | `PRIMER_AUTH__SESSION_SECRET` | auto-generated |
| Worker concurrency | `PRIMER_WORKER__CONCURRENCY` | `8` |
| Scheduler | `PRIMER_SCHEDULER__PROVIDER` | `in_memory` |

```callout:info
When `PRIMER_DB__PROVIDER` is unset, primer defaults to an embedded
SQLite database at `~/.primer/db/data.sqlite`. This is fine for a
single-developer install; use Postgres for anything shared or
multi-process.
```

## Full variable reference

```ref:reference/env-vars
Every PRIMER_* variable, its default, and what it controls.
```
