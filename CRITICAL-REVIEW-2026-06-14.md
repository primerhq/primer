# Primer - Whole-Project Critical Review

**Date:** 2026-06-14
**Method:** 8 parallel subsystem reviewers (execution core; API/auth/storage/models; workspaces/runtime; LLM/knowledge/vector; channels/triggers/bus; toolsets/harness/mcp; console UI; tests/docs/delivery), plus controller spot-verification of the highest-impact claims and three issues observed empirically during a live Quickstart run.
**Dimensions:** completeness, architecture, scalability, maintainability, reliability, user experience.

> Confidence tags below: **[confirmed]** = I verified in code or hit it live; **[reported]** = a reviewer found it, plausible, not independently re-verified; **[corrected]** = a reviewer's severity I adjusted after verifying.

---

## 1. Executive summary

Primer is an ambitious, genuinely well-architected platform. The core thesis (context-optimized microagents + park/resume + provider pattern) is implemented with real engineering depth: the claim machine has correct fencing and atomic release, the park/resume model is versioned and fails closed, the provider pattern is applied consistently, and the developer documentation is exceptional. The unit test suite (~5,200 tests) is green and fast.

But it is **not yet production-hardened**. The review surfaced a coherent cluster of issues that all point the same direction: **the system is built for correctness in the happy path and single-operator scale, and has systematic gaps in (a) failure isolation, (b) multi-worker/at-scale behavior, and (c) the delivery/operability surface.** The single most important issue - no hard timeout on the LLM call - I reproduced live: a flaky upstream pinned worker slots and wedged session dispatch until restart.

**Overall verdict by dimension:**

| Dimension | Rating | One-line |
| --- | --- | --- |
| Architecture | **Strong** | Clean patterns, good boundaries; a few god-objects accreting. |
| Completeness | **Good, with honest stubs** | Core is real; several advertised features are 501/stub (workspace pause/resume, search_collection, podman/containerd, S3/fs artifacts). |
| Reliability | **At risk** | No LLM/subprocess timeouts; several TOCTOU/double-resume races; auth gaps. The top priority area. |
| Scalability | **At risk** | Pervasive JSONB sequential scans (no expression indexes); many full-table scans and 200-row caps on hot paths. |
| Maintainability | **Mixed** | Great docs + fakes; but god-objects (pool.py, app.py, app.jsx, graphs.jsx) and a rotting e2e suite. |
| User experience | **Mixed** | Solid core operator flows; mock data in production views, no WS reconnect, a polling storm, and onboarding gaps (1-line README, wrong example config). |

---

## 2. Top cross-cutting issues (fix first)

### P0-1. No hard timeout on the LLM streaming call - a hung upstream pins a worker forever **[confirmed, observed live]**
Found independently by the execution-core and LLM reviewers, and reproduced during the Quickstart: `run_agent_turn` (`primer/agent/loop.py:~116`), `ChatTurnRunner._run_llm_loop` (`primer/chat/executor.py:~493`), and every LLM adapter's `async for ... in stream` (`primer/llm/openchat.py`, `openrouter.py`, `ollama.py`, etc.) iterate the provider stream with no `asyncio.timeout`. The claim heartbeat keeps renewing the lease, so a stalled stream occupies a worker slot **indefinitely**, and (because of the LLM concurrency semaphore) blocks every queued request behind it. At `max_concurrency` slots this drains the pool to zero. Live, this manifested as sessions stuck at `turn_no:0`, `in_flight` pinned, and no new dispatch until I restarted the process.
**Fix:** wrap stream iteration in a configurable per-turn hard timeout (e.g. `Limits.stream_timeout_seconds`, default ~300s); on breach, cancel cleanly, release the lease, transition the session to `WAITING`/`failed`. Apply to all adapters + the agent/chat loops.

### P0-2. `max_concurrency` vs single-request backends has no guard **[confirmed, observed live]**
`Limits.max_concurrency` is required but its interaction with single-threaded backends (LM Studio, local Ollama) is undocumented and unguarded. Setting it > 1 against LM Studio (which serves one request at a time) queues requests the backend never runs concurrently; combined with P0-1's missing timeout, this is exactly the wedge I hit. **Fix:** document `max_concurrency: 1` for LM Studio/Ollama; add a startup warning when a single-request flavor has `max_concurrency > 1`; consider a `max_queue_depth` that fails fast instead of blocking.

### P0-3. Auth gaps **[confirmed]**
- **Workers router mounted without auth** (`primer/api/app.py:1421` - no `dependencies=auth_dep`, unlike every sibling router). `POST /v1/workers/{id}/drain` is publicly reachable - an unauthenticated caller can halt new-session acceptance. **[confirmed]**
- **`require_auth` silently returns `None` for WebSocket connections** (`primer/api/deps.py:~370`), typed as `User` so callers can't null-check; WS session/chat streams may skip authz. **[reported]**
- **MCP approval-gate bypass:** `primer/mcp/safety.py` defers approval checks to the dispatcher, but `invoke_exposed`/`invoke_one` never consult the `ApprovalResolver`. An allowlisted tool whose policy is `approval: required` executes **unconditionally** over MCP. **[reported, high-priority - verify]**

### P0-4. Embedder/collection dimension mismatch caught only at query/store time **[confirmed, observed live]**
Changing the active embedder to one with a different output dim than an existing collection surfaces as a 400 at query time (`_internal_agents dimensions=768` vs a 384 query), and indexing silently no-ops after paying to embed all chunks (`primer/knowledge/indexing.py`). I hit this live (had to clear stale Lance data). **Fix:** detect dimension mismatch at config/activation time with a named `DimensionMismatchError`, before any embedding work; offer a re-index path.

### P0-5. Pervasive JSONB sequential scans - no expression indexes **[reported, structurally confirmed]**
`PostgresStorage` creates only a GIN `jsonb_path_ops` index (good for `@>` containment) but every `Q.where("field", v)` translates to `data->>'field' = $1`, which that index does **not** accelerate - so hot-path equality queries are full sequential scans. Concretely flagged: bearer-token lookup on **every** authenticated request (`token_hash`), session-status filters, channel inbound `channel_binding` lookups on **every** inbound message, startup session recovery `list()` with no status filter (full deserialize of all ended rows - OOM risk on mature DBs). **Fix:** add expression B-tree indexes on the enumerable hot fields (`token_hash` unique, session `status`, channel binding ids), and filter startup recovery by live status.

---

## 3. By dimension

### 3.1 Reliability (the priority area)
Beyond P0-1/P0-3/P0-4:
- **No timeout on git subprocesses** (`primer/workspace/local/state.py`, `runtime/primer_runtime/ops.py`) - a hung `git` (index.lock, NFS) freezes the event loop; the workspace-wide commit lock then freezes every concurrent session on that workspace. Same class as P0-1. **[reported]**
- **`init_commands` / `exec` with no deadline** (`workspace/local/backend.py`) - a runaway template init blocks workspace creation forever. **[reported]**
- **CorrelationStore double-resume race** (`primer/channel/correlation.py:55`) - `upsert_session` is a non-atomic read-modify-write with **no `UNIQUE(channel_id, anchor)` constraint**; concurrent inbound replies can insert two rows and publish a resume twice. **[reported]** Add the unique index + `ON CONFLICT`.
- **PostgresEventBus has no reconnect** (`primer/bus/postgres.py:155`) - if a LISTEN connection drops, parked sessions hang until the timeout sweeper fires; the scheduler has a reconnect loop, the bus doesn't. **[reported]**
- **Telegram approval cold-path** (`primer/channel/telegram/adapter.py:187`) - approve/reject button tags live only in an in-memory cache with no DB fallback, so after a restart the buttons are dead (silently swallowed). ask_user survives via CorrelationStore; approvals don't. **[reported]**
- **`on_delete` CDC hook fires before `storage.delete`** (`primer/api/routers/_crud.py:407`) - documented/intentional, but means a delete failure leaves a phantom CDC event (cache evicted/quota decremented for a row still alive). **[confirmed - design risk]** For deletes specifically, fire the hook after a successful delete.
- **`_table_ensured` keyed by `id(provider)`** (`primer/storage/postgres.py:~62`) - Python reuses object ids after GC; a recycled address can skip table creation. Use a `WeakKeyDictionary`. **[reported]**
- **Graph HITL reject may not drain remaining parked nodes** before failing (`primer/worker/pool.py:756`). **[reported]**
- **WSSandbox leaks** an aiohttp session + heartbeat tasks on `destroy()` (no `aclose()`); `append_file` catches bare `except` and can silently truncate on a transient read error. **[reported]**

### 3.2 Scalability
Beyond P0-5:
- **Full-table Chat scans on every inbound channel message** (`primer/channel/chat_router.py:79`, `discord/adapter.py`) - `_find_thread_chat` scans all chats with no predicate; degrades linearly with chat count. The CorrelationStore already exists to serve this but isn't queried. **[reported]**
- **O(N) chat-history scan on every claim** (`primer/chat/dispatch.py:451`) - loads the whole message log per turn; quadratic on long chats. Store a `next_unprocessed_seq` cursor. **[reported]**
- **MCP routing map rebuilt per `tools/call`** (`primer/mcp/dispatch.py`) - enumerates the entire tool catalogue (hundreds of provider calls) on every request; cache it keyed on `McpExposure.updated_at`. **[reported]**
- **200-row caps on hot paths** that degrade exactly under load: `list_triggers`/`list_subscriptions` (silent truncation at 201), parked-session timer/timeout sweeps (`bus/scheduler_tasks.py`), workspace probe reconciliation, `list_workspace_sessions` (loads all then slices). **[reported]**
- **Per-chunk embed loop** in `index_document` (one round-trip per chunk; the heavier `DocumentIngester` batches correctly - the light path doesn't). **[reported]**
- **`RateLimiter.acquire` busy-waits** at 100ms (`coordinator/postgres.py:136`) - N waiters x 10 qps against the DB; the LISTEN-based fix is already noted as deferred. **[reported]**
- **UI polling storm:** the App shell fires ~6-7 concurrent 5s polls regardless of page, and the dashboard adds ~6 more (~13 streams), none gated on tab visibility. **[reported]**
- **Hardcoded 60s lease TTL** ignores `WorkerConfig.lease_ttl_seconds` (`primer/claim/postgres.py:204,255`). **[reported]**

### 3.3 Architecture
- **Strong:** consistent provider pattern; clean claim/park/resume layering; `make_crud_router` + hook pipeline; type-safe `Q` predicate builder; sound secret handling (`SecretStr` centralized at the storage chokepoint); the toolset `make_tool` + schema-validated examples.
- **God-objects accreting** (the main architectural debt): `WorkerPool` (1,867 lines - owns session/graph/chat/harness execution + resume + 6 executor factories + claim/heartbeat), `app.py` lifespan (~1,200 lines, no per-subsystem isolation - one failure aborts startup), `chat/executor.py` (1,298), `toolset/system.py` + `workspaces.py` (~1,700 each), `ui/app.jsx` (1,775 - router + polling + toast + mock-tweak system), `ui/graphs.jsx` (3,115). Each is a high-friction onboarding point. Extract executor factories / resume dispatch / an `AppDataContext` / per-subsystem `AsyncContextManager`s.
- **`EmbeddingProvider` lacks the `_coerce_config_type` validator** that `LLMProvider` has - first-match-wins union parsing can silently coerce a huggingface config into the first union arm. Audit all provider unions for the same gap. **[reported]**
- **Duplicated `_ok/_err/_err_from_validation` across 5 toolset files**; extract to `_helpers.py`. **[reported]**
- **Two hand-synced runtime protocol files** (`primer/workspace/runtime/protocol.py` vs `runtime/primer_runtime/protocol.py`) have already diverged - extract a shared package + CI equality check. **[reported]**
- **Yielding/`requires_session` detection by source-text introspection** (`inspect.getsource` + substring "Yielded"/"ctx.session_id") is fragile - on closures `getsource` returns the *enclosing* function, mis-classifying MCP exposure. Replace with explicit `yields=`/`requires_session=` flags on `make_tool`. **[reported - notable]**

### 3.4 Completeness
- **Honest, but several advertised features are stubs/501:** workspace `pause`/`resume` (501), `system::search_collection` + `refresh_collection` (is_error stubs - the primary RAG tool for the `system` toolset), `podman` + `containerd` backends (raise ConfigError), S3/filesystem artifact backends (raise at construction but are valid enum values an operator can set), released-artifact install (deferred - source-only). AGENTS.md is commendably honest about most of these. **[reported, several confirmed]**
- **Cross-process rehydration missing for sandbox (container/k8s) workspaces** (`SandboxWorkspace.get_session` returns in-memory only; backends' `list()` returns in-memory only) - a session created by the API process is dropped by a worker process, and all workspaces vanish from the API after a restart. The local backend handles this; the non-local backends don't. This is the biggest completeness gap for non-local deployment. **[reported]**
- **`inform_user` is a silent no-op on the chat surface** (`chat/dispatch.py:385`) - documented deferral, but the tool's contract says it delivers. **[reported]**
- **4 declared Prometheus metrics have no writers** (flat-zero dashboards). **[reported]**

### 3.5 Maintainability
- **Strengths:** excellent `docs/dev` (7 architecture + 14 subsystem docs, hygiene-enforced), honest vision series, strong test fakes/seams, AGENTS.md.
- **The e2e suite is rotting** - **the most important maintainability finding.** ~372/865 e2e tests fail against current code (API drift: e.g. `Collection` now requires `search_provider_id`), the suite has been unmaintained since ~2026-05-25, and `tests/e2e/.state/backlog.md` still marks ~793 as "passed" - a **misleading green signal** embedded in the repo. As-is it is nearly valueless as a regression net. (Note: this is *not* caused by the recent dependency upgrade - I verified the upgrade changed zero `primer/` source and produced no crash signatures; the failures are pre-existing contract drift.) **[confirmed]** Re-establish a green baseline and date-stamp "last verified clean."
- **No CI pipeline** (no `.github/`, no `.gitlab-ci.yml`) - the 90% coverage gate, em-dash hygiene, and doc checks are all opt-in/local. **[confirmed]** A minimal Actions workflow running the narrowed sweep would pay for itself immediately.
- **UI "tests" are source-string greps** (`assert "useViewport" in src`), not behavioral; UI excluded from the coverage gate. **[reported]**
- **Heavy inline-style density** in the large jsx files defeats the existing CSS-token system. **[reported]**

### 3.6 User experience
**Operator console:**
- **Mock/fixture data leaking into production views** (`ui/app.jsx`): `window.MOCK.buildSessions` drives worker session-counts and an agents-page subtitle; a 2s mock animation interval forces full-app re-renders in production. **[reported - high]**
- **Stale copy contradicting shipped features:** Graphs page subtitle "executor not yet shipped" and a session-detail "executor missing" chip, though graphs have been live for months. **[reported]**
- **No WebSocket reconnect/backoff** (`chats.jsx`, `session-detail.jsx`) - a transient blip permanently silences live streaming until manual reload. **[reported - high]**
- **Message loss on send when WS is closed** - composer clears regardless of send success. **[reported]**
- **Desktop modals lack `role="dialog"`/focus-trap/focus-return** (the mobile sheet has them) - pervasive a11y gap across 15+ modals. **[reported]**
- Knowledge pages don't auto-refresh while others poll at 5s (inconsistent staleness).

**Onboarding / operability (developer + operator UX):**
- **`README.md` is one line** - no quickstart, requirements, or pointers. **[confirmed]**
- **`config.example.yaml` uses a flat `db_host`/`db_port` format that AppConfig silently ignores** (`extra="ignore"`), so an operator following it starts on SQLite instead of Postgres with no warning; `docker/primer/entrypoint.sh` writes the same flat format. **[confirmed - operator footgun]**
- **API ergonomics:** three token-router error paths return bare `JSONResponse` instead of the RFC7807 envelope every other route uses; a few surprising required fields (e.g. `Collection.search_provider_id`, provider `limits`) lack guiding errors.

---

## 4. Suggested remediation roadmap

**Tier 0 - reliability + security (do first):**
1. Hard timeout on LLM streams + git subprocesses + exec/init (P0-1, 3.1). The LLM one is empirically the highest-impact.
2. Authenticate the workers router and WS connections; enforce approval gates on the MCP path (P0-3).
3. `UNIQUE(channel_id, anchor)` + `ON CONFLICT` on CorrelationStore; PostgresEventBus reconnect loop (3.1).
4. Document/guard `max_concurrency` for single-request backends (P0-2).

**Tier 1 - scale + correctness:**
5. Add expression B-tree indexes for hot JSONB fields; filter startup session recovery by live status (P0-5).
6. Dimension-mismatch detection at embedder activation (P0-4).
7. Replace hot-path full scans (channel Chat lookup via CorrelationStore; chat `next_unprocessed_seq` cursor; cache MCP routing map; paginate the 200-capped paths).
8. Cross-process rehydration for sandbox workspaces + backend `list()` from persistent state (3.4).

**Tier 2 - delivery + maintainability:**
9. Fix `README.md` and `config.example.yaml`/entrypoint to the nested format. Add a minimal CI workflow.
10. Re-baseline the e2e suite (triage the 372, date-stamp the backlog) or quarantine it honestly.
11. Replace `inspect.getsource` heuristics with explicit `yields`/`requires_session` flags.
12. Remove mock data from production UI paths; add WS reconnect; fix the stale copy.

**Tier 3 - structural:**
13. Decompose the god-objects (WorkerPool factories/resume; app.py lifespan into per-subsystem context managers; app.jsx data layer; split the largest toolset/UI files).

---

## 5. Caveats on this review
- Findings tagged **[reported]** come from subsystem reviewers reading the code; the highest-severity ones (MCP approval bypass, WS auth, CorrelationStore race, git timeouts) should be independently reproduced before remediation - a couple of "Critical" reviewer ratings were over-stated (e.g. `Q.where_null` is genuinely broken but has **zero callers**, so it's a latent Low, not a Critical).
- This review is breadth-first across ~90K LOC; it is a prioritized map of where to look, not an exhaustive bug list. Per-subsystem detailed findings (with more Medium/Low items) are available on request.

---

# Addendum: Concrete fixes (2026-06-14)

Per-issue implementation guidance, ordered by tier.

## Tier 0 - reliability + security

**P0-1 LLM/subprocess timeouts.**
- Add `stream_timeout_seconds: int = 300` to `Limits` (`primer/model/provider.py`).
- In each LLM adapter and in `run_agent_turn` / `ChatTurnRunner._run_llm_loop`, wrap the `async for ev in stream` in `async with asyncio.timeout(timeout):`. On `TimeoutError`: cancel the stream, emit a `ChatError(kind="timeout")`, let the turn fail cleanly so the worker releases its lease and the session goes `WAITING`/`failed` (not pinned).
- Same pattern for git/exec: `await asyncio.wait_for(proc.communicate(), timeout=...)` in `workspace/local/state.py`, `runtime/.../ops.py`, `workspace/local/backend.py` (init), and `exec` (a configurable per-call timeout, default e.g. 120s); kill the subprocess on breach.

**P0-2 max_concurrency guard.**
- At provider create/startup, if `flavor in {LMSTUDIO, ...single-request}` and `limits.max_concurrency > 1`, log a WARNING (and document `max_concurrency: 1` for LM Studio/Ollama). Optionally add `max_queue_depth` to fail fast instead of blocking.

**P0-3 auth.**
- `primer/api/app.py:1421`: add `dependencies=auth_dep` to the workers `include_router` (one-line fix).
- WS auth: in the chat/session WS handlers, authenticate the cookie/bearer on handshake; close with code 4401 if absent. Make `require_auth` return `User` for WS or raise.
- MCP approval gate: in `invoke_exposed` (`primer/mcp/dispatch.py`), before dispatch, resolve the `ToolApprovalPolicy` for `(toolset_id, bare_name)`; if `required`/policy-says-required, return `NotExposed(reason="approval_required")`. Or enforce at PUT time in `_validate_allowed_tools` so approval-gated tools can't be allowlisted at all (simpler, stricter).

**P0-4 dimension mismatch.**
- In `internal_collections` activation + `index_document` (`primer/knowledge/indexing.py`), before embedding, fetch the collection's stored dimension and compare to the active embedder's output dim (one probe embed cached). Raise a named `DimensionMismatchError` (422) with a clear message + a "re-index required" hint.

**3.1 CorrelationStore race / bus reconnect / telegram approvals.**
- Add a `UNIQUE (channel_id, anchor)` index on `channel_correlations`; switch `upsert_session` to storage-level `ON CONFLICT (channel_id, anchor) DO UPDATE`.
- `primer/bus/postgres.py`: wrap the subscription connection in a reconnect loop mirroring `PostgresScheduler._watch_channel` (re-acquire, re-`add_listener`, continue) with backoff.
- Persist approval-button tags in `CorrelationStore` (new `kind="approval"` keyed on tag) and add the cold-lookup fallback in Telegram `_resolve_tag`.

## Tier 1 - scale + correctness

**P0-5 indexes + startup recovery.**
- In Postgres DDL bootstrap, add expression B-tree indexes for hot fields, e.g. `CREATE UNIQUE INDEX CONCURRENTLY ... ON apitokens ((data->>'token_hash'))`, `... ON sessions ((data->>'status'))`, channel `((data->>'provider_id'),(data->>'external_id'))` (also fixes the channel uniqueness TOCTOU), and `(data->>'channel_binding'...)` paths used by inbound routing.
- Startup session recovery: pass a `Q` predicate filtering `status IN (RUNNING,PAUSED,CLAIMED)` instead of `list()`.

**Hot-path scans.**
- Channel `_find_thread_chat`: query `CorrelationStore` (kind=chat) by `(channel_id, thread_external_id)` instead of scanning all chats; write that correlation on chat create.
- Chat claim: store `next_unprocessed_seq` on the `Chat` row; replace `_find_next_user_message`/`_find_resume_reply` full scans with a bounded read from that cursor.
- MCP routing map: cache in `ExposureDeps` keyed on `McpExposure.updated_at` (short TTL); invalidate on update.
- Paginate the 200-capped paths (`list_triggers`, `list_subscriptions`, the parked-session sweeps, `list_workspace_sessions`) with the existing `while True: offset += 200` pattern, or add cursor params.
- `index_document`: batch all chunk embeds in one call (mirror `DocumentIngester`).

**Cross-process workspaces.**
- Implement `SandboxWorkspace.get_session` rehydration from the runtime's state files (mirror `LocalWorkspaceBackend._rehydrate_session`); implement backend `list()` to hydrate from docker inspect / k8s statefulsets / disk on startup; implement `DockerAdapter.get_sandbox`.

## Tier 2 - delivery + maintainability

- `README.md`: 50-80 lines (what it is, requirements, `uv run primer api`, Postgres via compose, links to CONTRIBUTING + console).
- `config.example.yaml` + `docker/primer/entrypoint.sh`: switch the `db_*` flat keys to the nested `db: {provider: postgres, config: {hostname, port, ...}}` form; same for any other flattened section.
- Add a minimal CI workflow (GitHub Actions) running the narrowed sweep + coverage + doc hygiene on push/PR.
- Replace `inspect.getsource` heuristics in `primer/toolset/internal.py` with explicit `yields: bool` / `requires_session: bool` flags on `make_tool`.
- Extract the duplicated `_ok/_err` toolset helpers into `primer/toolset/_helpers.py`.
- Console: remove `window.MOCK` usage from production render paths (confine to the design canvas); add WS exponential-backoff reconnect (reuse the `initialLoadedSeq` cursor); fix the "executor not yet shipped" stale copy; guard the composer-clear on send success.

## Tier 3 - structural

- Decompose god-objects incrementally: extract `ExecutorFactory` + `ResumeDispatcher` from `WorkerPool`; split `app.py` lifespan into per-subsystem `AsyncContextManager`s composed with `AsyncExitStack`; pull an `AppDataContext` out of `app.jsx`; split the largest toolset/UI files.

## Latent / low (verified)
- `Q.where_null`/`where_not_null` build `Op.EQ/NE` + None (broken `= NULL`) but have zero callers - rewrite to emit `Op.IS_NULL`/`Op.IS_NOT_NULL` to defuse the trap before someone uses them.

---

# Addendum 2: findings surfaced while fixing the e2e suite (2026-06-14)

These came out of driving the e2e suite to green; they are real and were NOT
in the original review.

- **`auto_start=False` does not keep a session inert.** `session_factory`
  calls `claim_engine.upsert(SESSION, sid)` unconditionally (session_factory.py:306),
  so even a session created with `auto_start=False` is enqueued; the worker
  claims it and runs a (trivial, no-instruction) turn to completion. Intent of
  `auto_start=False` is "create but do not run until explicitly started." Either
  gate the upsert on `auto_start`, or have the worker skip CREATED sessions with
  no pending work. (Worked around in the injected-park e2e tests by waiting for
  the auto-run to finish before injecting.)
- **Missing route: `GET /v1/chats/{id}/tool_approval/pending`.** The handler was
  never registered in `make_tool_approval_router()`, so the path falls through to
  FastAPI's default `{"detail":"Not Found"}` (NOT the RFC7807 problem-details
  envelope). e2e T0836 is marked `xfail(strict)` pointing at the gap. Sessions
  have the equivalent endpoint; chats do not.
- **Two real bugs were fixed inline while fixing e2e:** empty-string entity id
  handling (kept the documented autogen) and a non-RFC7807 409 envelope in the
  reference-block (now `ConflictError`). See commit 3b9c4c4a.
- **Test hermeticity:** the unit suite intermittently HANGS at ~99% on a single
  non-hermetic test that makes a real external `:443` call (not HF Hub - persists
  under `HF_HUB_OFFLINE=1`). It passes when the endpoint is fast (5211 green in
  ~107s) and hangs when slow. Find and mock/skip that test so the suite is
  deterministic + CI-safe.
