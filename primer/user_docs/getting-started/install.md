---
slug: install
title: Install primer
section: getting-started
summary: How to install primer and start the API server with uv or Docker.
---

## Requirements

Primer targets Python 3.13 on Linux and macOS. Windows works via WSL2. A working `git` install is required for workspace features.

```callout:warning
Allocate at least 4 GB of free memory before starting primer. The workspace pool and the LLM call buffer share the same address space, and tight memory shows up as flaky tool calls before it shows up as an out-of-memory crash.
```

## Install and start

Two supported install paths:

```code-tabs:bash,docker
--- bash
git clone https://github.com/codemug/primer.git
cd primer
uv sync
uv run primer api
--- docker
docker pull ghcr.io/codemug/primer:latest
docker run -p 8000:8000 \
  -v $HOME/.primer:/data \
  ghcr.io/codemug/primer:latest
```

`primer api` starts the FastAPI HTTP server and an in-process worker pool together. Once it is running, open `http://localhost:8000/console/` for the dashboard.

## Config file

By default primer auto-loads `~/.primer/config.yaml` if it exists, then falls back to built-in defaults (embedded SQLite at `~/.primer/db/data.sqlite`). Pass an explicit path with `--config`:

```code-tabs:bash
--- bash
uv run primer api --config /etc/primer/config.yaml
```

Every config field is optional. Any field you omit can be supplied via a `PRIMER_*` environment variable instead.

## Storage

Out of the box primer uses an embedded SQLite database -- no external services required. To use PostgreSQL set the storage variables before starting:

```code-tabs:bash
--- bash
export PRIMER_DB__PROVIDER=postgres
export PRIMER_DB__CONFIG__HOSTNAME=localhost
export PRIMER_DB__CONFIG__USERNAME=primer
export PRIMER_DB__CONFIG__PASSWORD=secret
export PRIMER_DB__CONFIG__DATABASE=primer
uv run primer api
```

For production deployments with separate API and worker processes, run `primer api --no-worker` and `primer worker` as two processes against the same shared storage.

```callout:tip
Use a PostgreSQL scheduler backend (`PRIMER_SCHEDULER__PROVIDER=postgres`) when running multiple worker processes. The default in-memory scheduler is not shared across processes.
```

## Full reference

For every flag and environment variable:

```ref:reference/cli
primer CLI subcommands and flags.
```

```ref:reference/env-vars
All PRIMER_* environment variables and their defaults.
```
