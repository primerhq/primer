---
slug: environments
title: Dev and production environments
section: getting-started
summary: SQLite single-process dev vs Postgres plus workers production, and the workspace backends available in each.
---

## The same binary, different posture

Primer does not ship distinct dev and prod binaries. The same
`uv run primer api` command serves both environments. The posture is
determined by the config and env vars you provide.

| Concern | Dev | Production |
|---|---|---|
| Storage | embedded SQLite | Postgres recommended |
| Runtime mode | `api+worker` (one process) | `api` + `worker` as separate processes |
| Scheduler | `in_memory` | `postgres` |
| Auth | optional (`PRIMER_AUTH__ENABLED=false`) | required, cookie-secure |
| Session secret | auto-generated (resets on restart) | pinned in a secrets manager |
| Doc lint | log and skip on error | `PRIMER_USER_DOCS_STRICT=1` to block startup |
| Workspace backend | local (host filesystem) | container or Kubernetes |

## Dev quickstart

The minimal dev loop runs everything in one process with no external
dependencies:

```code-tabs:bash
--- bash
# Strict doc lint -- refuses to start when any user doc has a lint
# error. Recommended for authors.
export PRIMER_USER_DOCS_STRICT=1

uv run primer api
```

Primer prints the bind address and the doc-lint summary, then serves
on `http://0.0.0.0:8000`. Content edits to any `.md` file under
`primer/user_docs/` take effect on the next request with no restart.

Auth defaults to enabled; in a purely local dev setup you can disable
it to skip the login page:

```code-tabs:bash
--- bash
export PRIMER_AUTH__ENABLED=false
uv run primer api
```

## Production wiring

Two changes are required vs dev: a non-embedded database and a stable
session secret.

```code-tabs:bash
--- bash
export PRIMER_DB__PROVIDER=postgres
export PRIMER_DB__CONFIG__HOSTNAME=db.example
export PRIMER_DB__CONFIG__USERNAME=primer
export PRIMER_DB__CONFIG__PASSWORD=secret
export PRIMER_DB__CONFIG__DATABASE=primer

export PRIMER_AUTH__SESSION_SECRET=$(openssl rand -hex 32)
export PRIMER_AUTH__ENABLED=true
export PRIMER_AUTH__COOKIE_SECURE=true

# Scheduler: use postgres so the worker and api processes share state.
export PRIMER_SCHEDULER__PROVIDER=postgres

# Split API and worker into separate processes for independent scaling.
export PRIMER_RUNTIME_MODE=api
uv run primer api --no-worker
```

Run the worker pool in a separate process or container:

```code-tabs:bash
--- bash
uv run primer worker --config /etc/primer/config.yaml
```

```callout:warning
The session secret rotates all active sessions when it changes. Pin
the value across deploys using a secrets manager -- otherwise users
see authentication errors on every push.
```

## Workspace backends

Workspaces are the isolated sandboxes agents run inside. Three
backends are available:

- **Local** -- agents run directly on the host filesystem. Good for
  dev; no container runtime required.
- **Docker / container** -- each workspace is a container. Requires
  Docker or a compatible runtime on the host.
- **Kubernetes** -- each workspace is a Pod. Requires an in-cluster
  deployment or a configured kube context. Suitable for production
  multi-tenant installs.

```ref:features/workspaces
Configure workspace providers, create templates, and manage instances
from the console.
```

## Managing config across environments

Most teams keep one YAML config per environment and inject secrets
via env vars:

```code-tabs:bash
--- bash
# Local dev
uv run primer api --config /etc/primer/dev.yaml

# Production (secrets come from env)
uv run primer api --config /etc/primer/prod.yaml
```

```ref:reference/env-vars
Full list of PRIMER_* variables, their defaults, and what each
controls.
```
