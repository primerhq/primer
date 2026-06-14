# Primer

Primer is a context-first, batteries-included agent orchestration platform. It
ships LLM providers, workspaces, directed graphs, knowledge collections,
channels, triggers, and vector search as integrated first-class primitives --
everything an agent deployment needs, wired together from the start.

The central design bet: a small model given a clean, purpose-built context can
rival a much larger model on a narrow task. Every Primer feature exists to keep
each agent's working context tight and accurate.

## What you can build

- **Agents and chats** -- run multi-turn conversations against configurable
  agents; switch agents mid-chat without losing shared history.
- **Directed graphs** -- wire agents into graphs (producer-judge loops,
  fan-out/fan-in, conditional branches) that run as structured workflows.
- **Knowledge and collections** -- ingest documents into vector collections;
  agents retrieve only the relevant chunks, not the whole corpus.
- **Channels** -- bridge agents to Slack, Telegram, and Discord; agents can ask
  questions, request approvals, and be triggered from a channel message.
- **Workspaces** -- materialised sandboxes (local, container, or Kubernetes)
  that give agents a persistent filesystem and git-backed state.
- **Triggers** -- schedule work or fire on events; agents park and resume
  around slow tools or human approvals without blocking compute.
- **MCP** -- expose all platform tools over the Model Context Protocol so
  external agents and MCP clients can use Primer's full surface.
- **Harnesses** -- package a tuned set of agents, graphs, and collections into
  a git-backed versioned bundle; deploy it anywhere with one command.

## Quickstart

**Requirements:** Python 3.13, `uv`, and a Postgres instance (see
`docker-compose.yml` for a one-command local setup).

```bash
# 1. Clone and install
git clone https://github.com/codemug/primer.git
cd primer
uv sync

# 2. Start Postgres (optional: skip if you already have one)
docker compose up -d postgres
# or: podman compose up -d postgres

# 3. Configure
cp config.example.yaml config.yaml
# Edit config.yaml: at minimum set db.config.password to match your Postgres

# 4. Start the API (includes an in-process worker by default)
uv run primer api --config config.yaml

# 5. Verify
curl http://localhost:8000/v1/health
# -> {"status":"ok"}
```

Open the operator console at `http://localhost:8000/console/`.

### Zero-config single-user mode

If you have no Postgres instance and just want to try Primer locally, omit the
`--config` flag. Primer falls back to an embedded SQLite database at
`~/.primer/db/data.sqlite`. Worker scheduling is in-memory only in this mode;
it is not suitable for multi-process or production deployments.

```bash
uv run primer api
```

### Docker / Podman

The included `docker-compose.yml` starts Postgres plus the Primer application
container together. AppConfig is driven entirely by `PRIMER_*` environment
variables; no config file is needed:

```bash
docker compose up -d        # start postgres + primer
# or:
podman compose up -d
```

The application is available on port 8765 by default (mapped in compose).

## Configuration

`config.example.yaml` documents every supported field. Copy it to
`config.yaml` (gitignored by convention) and adjust the values for your
environment. The most common change is the database password.

The CLI reads the file with `yaml.safe_load` and passes the result to
`AppConfig`. Key sections:

- `db` -- storage backend (nested `provider` + `config` block; see the example
  for the correct Postgres shape). Omit to use embedded SQLite.
- `scheduler` -- background-job scheduler. Use `provider: postgres` for
  production; omit for in-memory single-process use.
- `vector_store` -- required for collections and semantic search.
- `worker` -- pool concurrency and lease knobs.
- `auth` -- cookie-based session auth (enabled by default).
- `observability` -- OTEL traces and Prometheus metrics.

Environment variables override file values. Every `AppConfig` field maps to a
`PRIMER_<FIELD>` env var; nested fields use double-underscore separators (for
example `PRIMER_DB__CONFIG__PASSWORD`).

## Documentation

- **Operator docs** -- built into the console under the Help menu; also served
  at `/docs` when the server is running.
- **Agent-usage docs** -- `docs/agents/` -- how to drive a running Primer
  instance from an AI agent over MCP.
- **Developer docs** -- `docs/dev/` -- architecture patterns, subsystem
  references, and the contributing guide. Start at `docs/dev/README.md`.

## Contributing

Read `AGENTS.md` first. It describes the coordinator/subagent workflow, the
project layout, the Definition of Done, how to run the test suites, and the
hard rules (no em-dash characters, no `Co-Authored-By` footers, conventional
commit messages).

Quick setup:

```bash
uv sync
docker compose up -d postgres
# run the narrowed unit sweep (excludes e2e/distributed/ui_e2e):
uv run pytest tests/ -q --ignore=tests/distributed --ignore=tests/ui_e2e \
  --ignore=tests/e2e --ignore=tests/integration --ignore=tests/llm
```
