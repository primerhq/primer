---
slug: environments
title: Dev and production environments
section: getting-started
summary: How to run primer in dev (strict lint, hot-reload) vs production (lenient lint, signed cookies).
---

## The two modes

Primer does not ship distinct dev and prod binaries; the same `uv
run primer api` command serves both. The mode is implied by the env
vars and flags you set.

| Concern | Dev | Production |
|---|---|---|
| Doc lint errors | block startup | log and skip the offending doc |
| Auth | optional (no-auth dev mode) | required, signed-cookie session |
| Storage | embedded SQLite | Postgres recommended |
| Observability | console logs | OTEL + Prometheus |
| Worker pool | in-process | dedicated worker process |

```callout:tip
None of these settings is one-way. Flip the flag and restart;
nothing migrates state. The same TOML can serve both environments
with overrides applied via env vars.
```

## Dev quickstart

The minimal dev loop runs everything in one process with no
external dependencies:

```code-tabs:bash
--- bash
# 1. Strict doc lint -- refuses to start if any user doc has a
#    lint error. Set in dev so authoring mistakes surface at
#    startup instead of silently dropping the doc.
export PRIMER_USER_DOCS_STRICT=1

# 2. Run.
uv run primer api
```

The CLI prints the bind address (`http://0.0.0.0:8000` by default)
and the doc lint summary. Edit any `.md` file under
`primer/user_docs/` and the next request to the docs page reads it
fresh -- no restart needed for content changes.

## Production wiring

Two changes vs dev: a non-embedded database and a real session
secret.

```code-tabs:bash
--- bash
export PRIMER_DB__PROVIDER=postgres
export PRIMER_DB__DSN=postgres://primer@db.example/primer
export PRIMER_SESSION_SECRET=$(openssl rand -hex 32)
export PRIMER_AUTH__REQUIRE_AUTH=true

# Optional: dedicate this process to serving the API and run the
# worker pool in a separate process.
export PRIMER_RUNTIME_MODE=api

uv run primer api
```

```callout:warning
The session secret rotates every operator out of an active session
when it changes. Pin the value across deploys (use a secret
manager); otherwise users see 401s on every push.
```

## Switching between them

Most teams keep one TOML per environment and inject it via
`PRIMER_CONFIG_PATH`. The env vars then supply the secrets that
must not live in the TOML.

```code-tabs:bash
--- bash
# Switch context locally:
export PRIMER_CONFIG_PATH=/etc/primer/dev.toml
uv run primer api

# Switch the same shell to a staging snapshot:
export PRIMER_CONFIG_PATH=/etc/primer/staging.toml
uv run primer api
```
