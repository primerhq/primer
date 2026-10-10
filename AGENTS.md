# AGENTS.md - Contributing to primer (for AI agents)

You are an AI agent working ON the primer codebase. This file is your contract
for making changes. (If instead you were connected to a running primer
deployment over MCP and want to USE it, read
[skills/using-primer-over-mcp.md](skills/using-primer-over-mcp.md).)

---

## 1. The working model: coordinator + parallel subagents

Development on this repo uses a coordinator/worker split:

- The **main session is a coordinator**. It does design, planning, dispatch,
  verification, and merge. It does NOT implement tasks directly.
- Each task gets its **own branch off `main`** and its **own git worktree**, so
  multiple workstreams proceed in parallel without colliding.
- A **subagent implements the task** inside that worktree.
- When the subagent reports done, the coordinator **critically verifies**
  completeness and quality against the Definition of Done (section 4). If it
  passes, the coordinator **merges the branch into `main`**. If not, the
  coordinator **re-dispatches the subagent** with specific fix instructions and
  re-verifies. Reviews are not rubber stamps: read the actual diff, run the
  tests, and reject work that masks a problem instead of fixing it.

### Per-task git worktree flow

```bash
# create the task branch + an isolated checkout (sibling dir)
git worktree add -b feat/<task-slug> ../primer-<task-slug> main
# ... subagent works and commits inside ../primer-<task-slug> ...
# after verification, from the main checkout:
git merge --no-ff feat/<task-slug>
git worktree remove ../primer-<task-slug>
git branch -d feat/<task-slug>
```

The Agent tool's `isolation: "worktree"` option automates the create/cleanup of
the per-task worktree; prefer it when dispatching an implementer.

Constraints that always hold: branch off `main`, never force-push `main`,
conventional commit messages with NO `Co-Authored-By` footer, stage only the
files a task touches (never `git add -A`).

---

## 2. Project setup and structure

- **Stack:** Python 3.12, `uv`, FastAPI, asyncio, Postgres + pgvector, a
  vanilla-React (JSX, no build step) console.
- **Setup:** `uv sync --all-extras` (the optional backends - huggingface,
  lance, channels, docker, kubernetes - live behind extras; the dev
  and test env needs them all); Postgres via `docker compose up -d postgres`; run with
  `uv run primer api` (starts the API plus an in-process worker). A dogfood
  instance is expected to stay healthy on `:9000` between tasks.
- **Layout:**
  - `primer/<subsystem>/` - the backend, one package per subsystem: `api`,
    `model`, `agent`, `session`, `knowledge`, `speech`, `worker`, `claim`,
    `graph`, `workspace`, `channel`, `toolset`, `llm`, `embedder`, `vector`,
    `trigger`, `bus`, `mcp`, `harness`, `storage`, `bootstrap`, and more.
  - `ui/` - the workspace shell (JSX components, `window`-exported
    surfaces, one URL grammar; no page router).
  - `tests/` - `tests/<subsystem>/` unit tests, `tests/e2e/` end-to-end against
    a live server, `tests/ui_e2e/` Playwright, `tests/distributed/`,
    `tests/docs/` hygiene.
  - `docs/dev/` - the authoritative developer reference (architecture +
    subsystems). `docs/agents/` - agent-usage docs served as internal AI docs
    (ingested into the `_internal_ai_docs` collection; see
    `docs/agents/_README.md`). The operator-facing console docs now live in a
    separate external repo (the `primerhq.github.io` Pages site), not in this
    tree.
  - `skills/` - guidance for agents using a primer deployment (not contribution).

---

## 3. Read before you change anything

Start at [docs/dev/README.md](docs/dev/README.md) for the doc-set map and
subsystem dependency graph, then read
[docs/dev/CONTRIBUTING.md](docs/dev/CONTRIBUTING.md) (required reading order,
PR conventions, common pitfalls).

Before changing a subsystem, read the cross-cutting patterns in
[docs/dev/architecture/](docs/dev/architecture/) that it touches:

- [provider-pattern](docs/dev/architecture/provider-pattern.md) - the
  discriminated-config + registry + factory shape used by every provider.
- [storage](docs/dev/architecture/storage.md) - the `Storage[T]` abstraction,
  lazy per-model tables, JSONB layout, and the query model.
- [rest-api](docs/dev/architecture/rest-api.md) - `make_crud_router`, the six
  CRUD ops, and the RFC7807 `ProblemDetails` error envelope every path returns.
- [claim-machine](docs/dev/architecture/claim-machine.md) and
  [worker-system](docs/dev/architecture/worker-system.md) - leases, claiming,
  the park/resume model.
- [observability](docs/dev/architecture/observability.md) and
  [auto-bootstrap](docs/dev/architecture/auto-bootstrap.md).

Then read the matching `docs/dev/subsystems/<name>.md` for the feature you are
working on.

---

## 4. Definition of Done

A change is NOT complete until every applicable track below is done. The
coordinator verifies each before merging. The detailed how-to for each track is
in [docs/dev/CONTRIBUTING.md](docs/dev/CONTRIBUTING.md) section 2; this is the
checklist.

1. **Backend** - models in `primer/model/`, storage wiring, REST routes under
   `primer/api/routers/` following the rest-api conventions, RFC7807 errors,
   observability hooks.
2. **UI** - implement or update the surface in the workspace shell
   (`ui/components/shell/`): the panel or overlay, its command-palette
   verb, URL state, mobile adaptation, loading/error/empty states, and
   toasts. A backend feature with no shell surface is incomplete.
3. **System tools** - if the change adds new functionality, expose it as new
   tools in the appropriate toolset under `primer/toolset/` (built with
   `make_tool`, registered for internal-collection ingestion, callable over
   `POST /v1/mcp`), so agents and MCP clients can use it.
4. **Docs** - update the agent-usage docs in `docs/agents/` plus the dev docs
   in `docs/dev/subsystems/` and `docs/dev/architecture/` as the change
   warrants. The operator-facing docs now live in a separate external repo (the
   `primerhq.github.io` Pages site); update them there when an operator-visible
   surface changes.
5. **Unit tests** - add or extend unit tests for new models, helpers, routes,
   and components.
6. **E2E tests** - add or extend end-to-end coverage under `tests/e2e/` (or
   `tests/ui_e2e/`) for the user-visible flow.
7. **Regressions** - run the suites, capture any regressions in existing tests,
   and fix them. Do not merge with a red suite; do not weaken a test to hide a
   real regression - fix the cause.

If a track is genuinely not applicable, say so explicitly with a one-line
reason rather than skipping it silently.

---

## 5. Running tests (read this - the e2e suite is CPU-exclusive)

- **Narrowed unit sweep** (must stay green at every commit, parallel, ~90s):

  ```bash
  uv run pytest tests/ -q --ignore=tests/distributed --ignore=tests/ui_e2e \
    --ignore=tests/e2e --ignore=tests/integration
  ```

  Add `-n0` to run a single module serially while debugging.

- **Live-Postgres suites** (`tests/claim`, `tests/scheduler`, `tests/storage`,
  `tests/coordinator`, `tests/vector`, plus the gated files that live with their
  subsystem: `LANE_FILES` in `tests/pg_gate.py`, currently one under `tests/bus`,
  one under `tests/worker` and one under `tests/session`; a static test fails a gated file the CI lane
  does not run) skip unless a database is configured. The
  single gate is `PRIMER_TEST_POSTGRES_URL=postgresql://user:pw@host:port/db[?schema=name]`;
  `PRIMER_TEST_PG_DSN` and `PRIMER_PG_TEST_DSN` are deprecated aliases that
  warn. The URL must name its port: the gate refuses one without (5432 on a dev
  host is often somebody's own database), and a static test fails any test
  that defaults a Postgres port (`or 5432`, an env lookup defaulting to
  `"5432"`). Test code reads the gate only through `tests/pg_gate.py` (a
  static test fails on any other `os.environ` read of a gate name); it is NOT the e2e
  server's Postgres capability, which uses `PRIMER_TEST_E2E_POSTGRES_DSN`, and
  it refuses to open on the database `primer_e2e`, whatever the schema,
  because the gated fixtures DROP tables and DELETE leases and some of their
  statements are unqualified, so a `?schema=` does not protect it. CI runs these in a dedicated
  `postgres` job (one pytest process per suite) against a
  `pgvector/pgvector:pg16` service (CI pulls it through `mirror.gcr.io`,
  Google's Docker Hub mirror, because Docker Hub rate-limits unauthenticated
  pulls) with `PRIMER_REQUIRE_POSTGRES_TESTS=1`: in
  that mode a Postgres-gated test that SKIPS fails, a module skipped at
  collection under those directories fails, the run refuses to start without the
  URL, and a suite in which no gated test passed fails. Locally, point at a
  throwaway container on a private port, never a shared or host Postgres:

  ```bash
  docker run --rm -d --name pg-test -e POSTGRES_USER=primer -e POSTGRES_PASSWORD=primer \
    -e POSTGRES_DB=primer_test -p 127.0.0.1:55512:5432 pgvector/pgvector:pg16
  PRIMER_TEST_POSTGRES_URL=postgresql://primer:primer@127.0.0.1:55512/primer_test \
  PRIMER_REQUIRE_POSTGRES_TESTS=1 uv run pytest tests/claim -o addopts= -n 0 -v \
    --timeout=120 --timeout-method=thread   # then tests/scheduler, storage, coordinator, vector
  ```

  Wait for a real query to succeed (the image's `pg_isready` answers during its
  init phase, before the final restart). New Postgres-gated tests get their
  gate and marker from `tests/pg_gate.py` (`postgres_marks`, `needs_postgres`,
  `require_postgres_url`), never from a raw `os.environ` read.

- **E2E suite saturates all CPU cores and must run EXCLUSIVELY.** Before any
  e2e run, check for an already-running one and kill it first:

  ```bash
  pgrep -af "pytest tests/e2e" | grep -v pgrep   # kill any match before starting
  ```

  Never run two e2e runs at once. Bring the environment up with
  `scripts/e2e/bringup.sh` (it reuses the shared dev Postgres on a separate
  `primer_e2e` database). Do NOT run `scripts/e2e/teardown.sh` with volume
  removal: it shares the Postgres container with the dogfood instance and will
  wipe it. Run targeted e2e with `-n0` and `PRIMER_RUN_E2E=1 PRIMER_E2E_PORT=8765`.

- Some tests are environment-gated: real-LLM tests need a reachable LM Studio
  (`LMSTUDIO_API_KEY` set; the provider must use `max_concurrency: 1` for a
  single-request backend); the Kubernetes workspace tests need a live cluster.

- After a code-changing task, restart the dogfood `uv run primer api` and
  confirm `/v1/health` returns 200.

---

## 6. Hard rules

- Never the em-dash character (U+2014) in committed files; the `tests/docs/`
  hygiene suite enforces this for `docs/dev/` and `AGENTS.md`.
- Every relative markdown link from `AGENTS.md` or `docs/dev/` must resolve.
- Keep `AGENTS.md` and the architecture/subsystem docs in sync with the code:
  the hygiene suite asserts the architecture and subsystem doc sets exist.
