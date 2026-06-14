---
slug: cli
title: Command-line interface
section: reference
summary: The primer CLI subcommands, their flags, and what each one does.
---

## Top-level

`primer` is a Typer application. Run `--help` on any subcommand for
the full flag list:

```code-tabs:bash
--- bash
uv run primer --help
# Primer microagents framework -- API + worker entrypoints.
#
# commands:
#   api      Serve the HTTP API (and an in-process worker by default)
#   worker   Run the worker pool (with a minimal health/workers HTTP surface)
#   init     Run first-time bootstrap. Idempotent; --force re-runs even if completed.
```

## primer api

Starts the FastAPI HTTP server via uvicorn. By default it also starts
an in-process worker pool (`runtime_mode=api+worker`). Pass
`--no-worker` to split API and worker into separate processes.

```code-tabs:bash
--- bash
# Default: API + worker in one process, auto-loads ~/.primer/config.yaml
uv run primer api

# Explicit config file.
uv run primer api --config /etc/primer/config.yaml
uv run primer api -c /etc/primer/config.yaml

# API only -- pair with a dedicated `primer worker` process.
uv run primer api --no-worker
```

### Flags

| Flag | Short | Default | Description |
|---|---|---|---|
| `--config PATH` | `-c` | `~/.primer/config.yaml` if it exists, else built-in defaults | Path to a YAML config file. |
| `--no-worker` | | off | Serve the API only; do NOT start the in-process worker pool. |

Config file discovery order: explicit `--config` > `~/.primer/config.yaml`
(if present) > built-in defaults (embedded SQLite at
`~/.primer/db/data.sqlite`).

YAML config fields map directly to `AppConfig`. Every field is
optional. Env vars (`PRIMER_*`) override missing YAML fields; a
`--config`-supplied YAML wins over env vars.

```callout:tip
For production deploys, run `primer api --no-worker` and `primer worker`
as two separate processes against the same shared storage. Scaling
workers becomes independent of scaling HTTP capacity.
```

## primer worker

Runs only the worker pool. A minimal HTTP surface (`/v1/health` and
`/v1/workers`) is still served for liveness/readiness probes. Pairs
with a `primer api --no-worker` process.

```code-tabs:bash
--- bash
# Default: auto-loads ~/.primer/config.yaml
uv run primer worker

# Explicit config.
uv run primer worker --config /etc/primer/config.yaml
uv run primer worker -c /etc/primer/config.yaml
```

### Flags

| Flag | Short | Default | Description |
|---|---|---|---|
| `--config PATH` | `-c` | `~/.primer/config.yaml` if it exists, else built-in defaults | Path to a YAML config file. |

## primer init

Runs first-time bootstrap. Idempotent: rows that already exist are
skipped. Pass `--force` to re-run the bootstrap even when the
completion marker is already set (useful for partially-failed runs).

```code-tabs:bash
--- bash
# Idempotent bootstrap against default config.
uv run primer init

# Explicit config file.
uv run primer init --config /etc/primer/config.yaml

# Re-run even if bootstrap already completed.
uv run primer init --force
```

### Flags

| Flag | Short | Default | Description |
|---|---|---|---|
| `--config PATH` | `-c` | `~/.primer/config.yaml` if it exists, else built-in defaults | Path to a YAML config file. |
| `--force` | | off | Re-run bootstrap even if it has already completed. |

Exit code `1` when any provider bootstraps with errors (printed to
stderr). Exit code `0` when all rows are created or skipped.
