# Contributing

This guide is the entry point for anyone adding a feature to Primer. It assumes
you have a clean checkout that boots zero-config (`uv run primer api` lands on
embedded SQLite at `~/.primer/db/data.sqlite` with auto-bootstrap). Read the
architecture docs in the order below, then satisfy the five-track completeness
checklist for any feature-bearing change.

## 1. Required reading order

Read the architecture docs in this order before contributing. Each one builds on
the layer beneath it, so the sequence matters more than the alphabetical order on
disk.

1. [Storage](architecture/storage.md) - the backend-agnostic `StorageProvider` /
   `Storage[T]` contract, the predicate language, the `Q[ModelT]` builder, and the
   lazy per-model table rule every persisted entity rides on.
2. [REST API](architecture/rest-api.md) - the `create_app` factory, middleware
   order, the RFC 7807 `ProblemDetails` envelope, `make_crud_router`, and the
   cookie-plus-bearer auth model.
3. [Auto-Bootstrap](architecture/auto-bootstrap.md) - the first-run provisioning
   seam, the reserved-id rows, and the router-layer protections that keep them
   immutable.
4. [Observability](architecture/observability.md) - tracing, the dedicated
   Prometheus registry, log correlation, and the turn-log writer family.
5. [Claim Machine](architecture/claim-machine.md) - the polymorphic `ClaimEngine`,
   the shared `leases` table, and the per-kind `ClaimAdapter` contract.
6. [Worker System](architecture/worker-system.md) - the three coordination ABCs
   (`Scheduler`, `ClaimEngine`, `Coordinator`), the `WorkerPool` dispatch loop, and
   the leader-elected background tasks.
7. [Provider Pattern](architecture/provider-pattern.md) - the ABC-plus-adapter
   shape shared across LLMs, embedders, cross-encoders, toolsets, vector stores,
   web search, and channels, plus the per-row registries.

After those, jump to the relevant subsystem doc under
[docs/dev/subsystems/](subsystems/) for the feature you are touching (for example
`subsystems/sessions.md`, `subsystems/knowledge.md`,
`subsystems/triggers.md`, `subsystems/channels.md`, `subsystems/ui-pages.md`).

## 2. Completeness checklist

Every feature-bearing contribution must satisfy these five tracks. A track that
genuinely does not apply must be marked "not applicable" in the PR description with
a one-line reason; it may not simply be omitted.

### Backend

- Declare the dependency tier: any new third-party dependency is either
  justified as core in [modularity](subsystems/modularity.md) or gated behind an
  extra with a `require_extra` guard and a capabilities row.
- Define the persisted shape as Pydantic models in `primer/model/` (an
  `Identifiable` subclass for anything stored).
- Honour the storage migration rules: per-model tables are created lazily on first
  handle use, serialisation goes through `dump_for_storage` so `SecretStr` fields
  round-trip as plaintext, and there is no Alembic step to add. New columns on the
  `system_state` singleton follow the existing additive shim.
- Wire the service or registry: add the per-model `Storage[T]` dependency in
  `primer/api/deps.py`, and for adapter-backed families add the factory branch and
  registry entry per the provider-pattern doc.
- Add REST routes under `primer/api/routers/` following the rest-api conventions:
  prefer `make_crud_router` and its declarative knobs (`scope_field`,
  `managed_by_field`, `references`, `cdc_kind`) over hand-rolled guards; mount under
  `_mount_routers` with `dependencies=[Depends(require_auth)]`; declare error
  responses with `common_responses(...)`.
- Return `ProblemDetails` for every error path. Raise `PrimerError` subclasses and
  let the registered handlers render the RFC 7807 envelope; never hand-build an ad
  hoc error body.
- Add observability hooks: `logger.exception` on `except` arms, metrics for
  latency-sensitive operations (bind new metrics to the dedicated registry and
  mirror them in `reset_for_test()`), and turn-log events at session and graph
  boundaries through `safe_append`.

### Frontend

- Add the page component under `ui/components/` and export it on `window`.
- Add its overlay name to `SH_OVERLAYS` in `ui/foundation/shell-url.js` and a mount
  for it in `ui/components/shell/sh-overlay-host.jsx`.
- Give it a verb label in `SH_OVERLAY_LABELS` (`ui/components/shell/sh-doc-host.jsx`);
  an overlay no verb opens is one a user cannot find, and the dual-render guard
  fails on it.
- Provide a mobile adaptation via `useViewport`.
- Render loading, error, and empty states for every `useResource`.
- Wire user feedback through `pushToast`.
- Confirm the page renders clean under `PRIMER_USER_DOCS_STRICT=1`.

### MCP tools

- Define the tool in the correct toolset under `primer/toolset/` with a complete
  `args_schema` and a documented result envelope.
- Register the toolset for internal-collection ingestion in the
  `primer/api/app.py` lifespan bootstrap.
- Verify the tool description is visible at
  `GET /v1/collections/<internal-id>/indexed_documents` after bootstrap.
- Verify the tool is callable via `POST /v1/mcp`.
- If the change adds no new operation, mark this track "not applicable" with the
  reason.

### Tests

- Unit tests for new Pydantic models or pure-function helpers.
- Router tests for new REST routes hitting the in-memory storage; `tests/conftest.py`
  provides `_FakeStorageProvider` / `_InMemoryStorage`, and `tests/api/conftest.py`
  provides the `app` and `client` fixtures.
- Component-render tests for new React components that have conditional rendering.
- End-to-end tests for user-visible flows under `tests/ui_e2e/` or `tests/e2e/`.
- The narrowed sweep stays green:

  ```bash
  uv run pytest tests/ -q --ignore=tests/distributed --ignore=tests/ui_e2e --ignore=tests/e2e --ignore=tests/integration
  ```

  The suite runs in parallel by default (`-n auto --dist loadscope` is baked
  into `addopts`), which takes the full unit sweep from roughly 7 minutes to
  about 90 seconds. `loadscope` keeps each module's tests on one worker
  because a few `tests/api` modules use module/class-scoped fixtures that do
  not survive being split across workers. To debug a single test serially,
  override with `-n0`.

- The live-Postgres suites run in their own CI lane. `tests/claim`,
  `tests/scheduler`, `tests/storage`, `tests/coordinator` and `tests/vector`
  contain tests that need a real database and skip without one, so the
  narrowed sweep above does not exercise them. The `postgres` job in
  `.github/workflows/ci.yml` does, against a `pgvector/pgvector:pg16` service
  (CI pulls it through `mirror.gcr.io`, Google's Docker Hub mirror, because
  Docker Hub rate-limits unauthenticated pulls),
  running each suite as its own pytest process (a hang kills only its own
  process, so one suite cannot erase the others' results). Gated files that
  live with their subsystem instead (`LANE_FILES` in `tests/pg_gate.py`:
  today one under `tests/bus` and one under `tests/worker`) each get their own
  step too, so a file that vanishes fails its own process; a static test fails
  any gated file that is in neither `LANE_DIRS` nor `LANE_FILES`. The one gate is
  `PRIMER_TEST_POSTGRES_URL` (`postgresql://user:pw@host:port/db`, optional
  `?schema=name`); the former `PRIMER_TEST_PG_DSN` and `PRIMER_PG_TEST_DSN` are
  deprecated aliases that warn. The URL must name its port; the gate refuses a
  port-less one, and a static test fails any test file that defaults a
  Postgres port, because 5432 on a dev host is often a developer's own
  database that the gated fixtures would drop tables in. Test code reads the
  gate in exactly one place,
  `tests/pg_gate.py`, and a static test fails if any other test file reads a
  gate name, or a constant aliasing one, from `os.environ`. A new gated test
  takes its marker and skip from that module (`postgres_marks`,
  `needs_postgres`, `require_postgres_url`). The gate is not the e2e server's
  Postgres capability: `tests/testconfig.yaml` expands `${VAR}` from the
  environment itself, and its example uses a different variable,
  `PRIMER_TEST_E2E_POSTGRES_DSN`. The gated fixtures are destructive (they DROP
  tables and DELETE leases, and some statements are unqualified, so a `?schema=`
  does not redirect them), so the gate refuses to open on the database
  `primer_e2e`, whatever the schema; it belongs to a live e2e server.

  The lane sets `PRIMER_REQUIRE_POSTGRES_TESTS=1`, the anti-silent-skip guard
  (`tests/conftest.py`). In that mode a Postgres-gated test that skips fails
  the run, a module under a lane directory that is skipped at collection fails
  it, the run refuses to start without the URL, and a suite in which no gated
  test passed fails. Check the `postgres lane: N Postgres-gated test(s)
  passed` line in each suite's log, not just the job status. A per-test
  `--timeout` (thread method) and a job `timeout-minutes` bound a hang, such as
  a leaked LISTEN watcher at teardown, to minutes; the lane runs with `-v` so the
  last node id printed before a timeout dump names the test that hung.

  To run it locally, use a throwaway container on a private loopback port,
  never a shared or host Postgres:

  ```bash
  docker run --rm -d --name pg-test -e POSTGRES_USER=primer -e POSTGRES_PASSWORD=primer \
    -e POSTGRES_DB=primer_test -p 127.0.0.1:55512:5432 pgvector/pgvector:pg16
  PRIMER_TEST_POSTGRES_URL=postgresql://primer:primer@127.0.0.1:55512/primer_test \
  PRIMER_REQUIRE_POSTGRES_TESTS=1 uv run pytest tests/claim -o addopts= -n 0 -v \
    --timeout=120 --timeout-method=thread   # then tests/scheduler, storage, coordinator, vector
  ```

  Wait for a real query to succeed before running: the image's `pg_isready`
  answers during its init phase, before the final restart.

### Docs

- Update the relevant subsystem doc under `docs/dev/subsystems/` and any
  architecture doc whose contract shifted.
- Update the agent-usage doc under `docs/agents/<feature>.md` when the
  agent-visible behaviour changes.
- The operator-facing docs now live in a separate external repo (the
  `primerhq.github.io` Pages site); update them there when an operator-visible
  surface changes.
- Update the MCP tool description for any new operation.

## 3. PR conventions

- Use conventional commit messages (`feat:`, `fix:`, `refactor:`, `chore:`,
  `docs:`).
- Do not add a `Co-Authored-By` footer.
- Never force-push to `main`.
- Keep the narrowed sweep (section 2, Tests) green at every commit, not just at the
  tip.
- In the PR description, list which of the five tracks were addressed and explain
  any track marked "not applicable".
- Never use em dash characters anywhere in commits, code, or docs; the docs hygiene
  suite rejects them.

## 4. Common pitfalls

- Do not poke private attributes across module boundaries. Why: reaching into
  another module's internals couples you to its implementation and breaks on
  refactor. How: use the public lookups instead, for example the worker pool shim's
  `workspace_id_for` and `workspace.state_path`.
- Do not hardcode `.state/` paths. Why: operators can override the workspace state
  template, so a hardcoded path silently writes to the wrong place. How: read
  `workspace.state_path` so operator-overridden templates keep working.
- Do not invent new `ProblemDetails` shapes. Why: the UI and CLI depend on the one
  stable RFC 7807 envelope and a bespoke shape breaks their error handling. How:
  route exceptions through `to_problem_details` in
  `primer.observability.turn_log_writer` (re-exported via `primer.api.errors`).
- Do not use em dash characters. Why: the tests/docs hygiene suite rejects them and
  the build fails. How: use hyphens, semicolons, or sentence breaks instead.
- Do not stage spec or plan files. Why: `docs/superpowers/` is gitignored and those
  artifacts are not part of the tracked source. How: keep specs and plans out of
  `git add`; commit only the shipped code, tests, and dev/operator docs.
- Do not inline secrets in tests. Why: committed keys and tokens are a leak and the
  hygiene suite flags them. How: read API keys and bearer tokens from env vars and
  skip the test when the var is unset.
- Do not leave a `SqliteStorageProvider` open at the end of a test. Why: an unclosed
  aiosqlite connection is finished by the garbage collector inside whichever test
  runs next, where its worker thread dies with "Event loop is closed" and the
  warning lands on the wrong test; one still referenced at exit keeps pytest from
  exiting. How: build the provider in a fixture that yields and awaits `aclose()`,
  or push `provider.aclose` on the `async_closers` fixture from a helper. The
  autouse guard in `tests/conftest.py` fails the test that leaks and names the
  `initialize()` call.

## 5. Where to find things

- `tests/conftest.py` - the in-memory `_FakeStorageProvider` and `_InMemoryStorage`
  helpers (plus `fake_storage_provider` and `fake_llm` fixtures).
- `tests/api/conftest.py` - the FastAPI test client fixture (`app`, `client`, and
  `raw_client`).
- `tests/session/test_dispatch_turn_log.py` - the capturing-writer test pattern for
  turn-log events.
- `tests/docs/test_docs_hygiene.py` - the doc-rot hygiene tests, including the em
  dash and secret checks.
- `scripts/docs_verifier.py` - the consolidation verifier.
- `scripts/audit_touch_targets.py` - the mobile touch-target audit.
