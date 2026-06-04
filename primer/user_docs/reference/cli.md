---
slug: cli
title: Command-line interface
section: reference
summary: The uv run primer subcommands, their flags, and what each one prints.
---

## Top-level

The `primer` CLI is a typer app. Subcommand discovery via the
standard `--help`:

```code-tabs:bash
--- bash
uv run primer --help
# usage: primer [OPTIONS] COMMAND [ARGS]...
#
# commands:
#   api      Run the API + (optionally) worker process
#   worker   Run a dedicated worker process
#   init     Initialise storage + bootstrap providers
```

## primer api

The default entry point. Runs uvicorn with the FastAPI app and
(by default) the in-process worker pool.

```code-tabs:bash
--- bash
# Default: API + worker in one process.
uv run primer api

# API only (pair with a dedicated `primer worker` process).
PRIMER_RUNTIME_MODE=api uv run primer api

# Custom port.
PRIMER_PORT=9000 uv run primer api

# Strict doc lint (refuses to start on user-doc lint errors).
PRIMER_USER_DOCS_STRICT=1 uv run primer api
```

```callout:tip
For production deploys, run `primer api` and `primer worker` as
two separate processes against the same shared storage. Scaling
workers becomes independent of scaling HTTP capacity.
```

## primer worker

Runs only the worker pool, no HTTP server. Pairs with a
separate API-only process.

```code-tabs:bash
--- bash
PRIMER_WORKER__POOL_SIZE=16 uv run primer worker
```

The worker process claims sessions from storage and dispatches
their tool calls. It does not serve HTTP, so the worker host
does not need a public network ingress.

## primer init

Initialises a fresh primer install. Creates the storage schema,
seeds the built-in providers, mints a default operator account.

```code-tabs:bash
--- bash
# Interactive prompt for admin credentials.
uv run primer init

# Non-interactive (CI/automation).
uv run primer init \
  --admin-username admin \
  --admin-password "$(openssl rand -hex 16)" \
  --confirm
```

The `--confirm` flag is required for non-interactive use; without
it the command prompts even when stdin is closed.

## primer harness install

Installs a harness from a git source.

```code-tabs:bash
--- bash
uv run primer harness install \
  --source https://github.com/codemug/harness-pr-reviewer \
  --branch main

# Inspect what would change without writing.
uv run primer harness install \
  --source https://github.com/codemug/harness-pr-reviewer \
  --dry-run
```

The dry-run mode parses the harness manifest, shows what entities
would be created, and exits without writing.

## Exit codes

Every subcommand follows the same convention:

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Generic error (config invalid, storage unreachable, etc.) |
| `2` | Wrong CLI usage (typer surfacing arg parse errors) |
| `130` | SIGINT (Ctrl+C) |

CI consumers should rely on the exit code, not on stdout/stderr
parsing.
