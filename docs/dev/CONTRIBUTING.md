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
`subsystems/sessions.md`, `subsystems/chats.md`, `subsystems/knowledge.md`,
`subsystems/triggers.md`, `subsystems/channels.md`, `subsystems/ui-pages.md`).

## 2. Completeness checklist

Every feature-bearing contribution must satisfy these five tracks. A track that
genuinely does not apply must be marked "not applicable" in the PR description with
a one-line reason; it may not simply be omitted.

### Backend

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
- Register the route in `ui/foundation/router.js`.
- Add the sidebar entry in `ui/components/chrome.jsx` when the page is top-level.
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
  uv run pytest tests/ -q --ignore=tests/distributed --ignore=tests/ui_e2e --ignore=tests/e2e --ignore=tests/integration --ignore=tests/llm
  ```

  The suite runs in parallel by default (`-n auto --dist loadscope` is baked
  into `addopts`), which takes the full unit sweep from roughly 7 minutes to
  about 90 seconds. `loadscope` keeps each module's tests on one worker
  because a few `tests/api` modules use module/class-scoped fixtures that do
  not survive being split across workers. To debug a single test serially,
  override with `-n0`.

### Docs

- Update the relevant subsystem doc under `docs/dev/subsystems/` and any
  architecture doc whose contract shifted.
- Add or update operator markdown under `primer/user_docs/<section>/<feature>.md`.
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
