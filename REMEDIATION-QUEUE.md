# Remediation task queue (coordinator/worktree model)

Companion to `CRITICAL-REVIEW-2026-06-14.md` (findings + concrete fixes) and
`AGENTS.md` (the coordinator + git-worktree working model + Definition of Done).
Each task = a branch off `main` + a git worktree + a subagent; coordinator
verifies (diff review, no em-dash, regression gate) then merges. Run e2e
EXCLUSIVELY (kill any existing `pytest tests/e2e` first; never two at once).

Every task's Definition of Done = the AGENTS.md checklist: backend, UI, system
tools, user+agent docs, unit tests, e2e tests, regressions captured+fixed,
primectl. Mark a track N/A with a one-line reason.

## Done
- working-model setup (AGENTS.md repurposed; MCP usage -> skills/) - commit 4e8380ab
- llm-timeout - configurable `Limits.request_timeout_seconds` (per-event stall
  timeout, default 300s) + `_iter_with_timeout` in all 6 adapters +
  `ProviderTimeoutError` + UI input + docs + 15 tests - MERGED 40357e2a
- e2e drift/structural buckets already fixed - commits 8cd80069, 9d7e3095,
  645fc29f, 3b9c4c4a, 573cb3e8, 6c367437 (see review addenda)

## Pending tasks

### failure-isolation  [reliability; greens e2e t0539/t0630/t0649/t0679]
Sessions stay RUNNING forever when an LLM call fails AND workspace IO also
fails: in `primer/session/dispatch.py` (~lines 379-430) the except arm tries
`writer.append(error_rec)`; if that throws, the exception escapes before
`_transition_session_status` sets ENDED. Ensure the session ALWAYS transitions
to a terminal state (ENDED/failed) even if the error-record write fails (wrap
the error-record write so its failure cannot prevent the transition; release the
lease). DoD: backend; unit test (fake LLM raises + workspace write raises ->
assert session ENDED, lease dropped); e2e t0539/t0630/t0649/t0679 green; docs if
behavior documented; UI/tools/primectl N/A.

### git-timeout  [reliability]
No deadline on git/exec subprocesses; a hung `git` (index.lock/NFS) or runaway
`init_command`/`exec` freezes the event loop and the workspace commit lock.
Add a GLOBAL default timeout in config (AppConfig in `primer/api/config.py`,
overridable via config.yaml / PRIMER_*), applied to git + exec subprocess calls
in `primer/workspace/local/state.py`, `primer/workspace/local/backend.py`
(init_commands + exec), and `runtime/primer_runtime/ops.py`. Kill the subprocess
on breach; surface a typed error. DoD: backend; config docs (env-vars + dev);
unit tests (a sleeping subprocess + tiny timeout -> raises); UI N/A; tools N/A;
e2e optional; primectl N/A.

### auth  [security; greens e2e WS-auth-adjacent]
Three gaps: (1) workers router mounted without `dependencies=auth_dep`
(`primer/api/app.py` ~1421) -> POST /v1/workers/{id}/drain is public; add the
auth dep. (2) `require_auth` returns None for WebSocket (`primer/api/deps.py`
~370) typed as User -> WS session/chat streams may skip authz; authenticate the
cookie/bearer on the WS handshake and close 4401 if absent. (3) MCP approval-gate
bypass: `invoke_exposed`/`invoke_one` (`primer/mcp/dispatch.py`) never consult
the approval policy, so an allowlisted tool with `approval: required` runs
unconditionally over MCP; enforce it (block at dispatch or refuse to allowlist).
DoD: backend; unit tests per gap; e2e if a flow exists; docs (mcp-exposure,
auth-and-tokens); UI N/A; primectl N/A.

### sessions-filter  [correctness; greens e2e t0321 + t0180]
(a) `GET /v1/sessions?graph_id=<id>` silently ignores graph_id (not implemented
in the list handler, `primer/api/routers/sessions.py` ~448) - implement it.
(b) `POST /v1/sessions/find` cursor pagination drops a mid-sorted item
(timestamp tie-break) - fix the cursor to use a stable tiebreaker (e.g. id) so
pagination is complete. DoD: backend; unit + e2e (t0321, t0180); docs if needed;
UI N/A unless the console filters by graph_id; primectl if it exposes the filter.

### chat-approval-pending  [completeness; un-xfail e2e t0836]
Missing route `GET /v1/chats/{id}/tool_approval/pending` - the handler was never
registered in `make_tool_approval_router()` so it 404s with the default FastAPI
body (not RFC7807). Implement it mirroring the session equivalent
(`get_chat_tool_approval_pending`). Remove the `xfail(strict)` on e2e t0836.
DoD: backend; unit + e2e (un-xfail t0836); docs (tool-approval); UI if the chat
view should surface pending approvals; tools/primectl N/A likely.

### auto-start  [correctness; hardens injected-park tests]
`auto_start=False` does not keep a session inert: `session_factory`
(`primer/workspace/session_factory.py` ~306) calls
`claim_engine.upsert(SESSION, sid)` unconditionally, so the worker claims+runs a
trivial turn. Gate the upsert (and lease enqueue) on `auto_start=True`, or make
the worker skip CREATED sessions with no pending work. Verify it does not break
the explicit-start path. DoD: backend; unit + e2e; docs (sessions); UI/tools/
primectl N/A.

### dim-mismatch  [reliability/UX]
Embedder/collection dimension mismatch (e.g. 384 vs 768) surfaces only at query
time (400) and indexing silently no-ops after embedding all chunks. Detect at IC
activation / collection use: compare the active embedder output dim to the
stored collection dim, raise a named `DimensionMismatchError` (422) with a
re-index hint BEFORE embedding. Files: `primer/knowledge/indexing.py`, IC config
activation route, `primer/catalog/`. DoD: backend; unit; docs
(semantic-search/internal-collections, user+agent); UI error surfacing if
applicable; e2e optional; tools/primectl N/A.

### scale-indexes  [scalability]
Postgres storage has only a GIN index; `data->>'field' = $1` equality queries
are sequential scans on hot paths (bearer token_hash on every request, session
status, channel binding ids). Add expression B-tree indexes
(`CREATE INDEX CONCURRENTLY`) for the enumerable hot fields (token_hash unique,
session status, channel provider_id+external_id, channel_binding). Also: startup
session recovery `list()`s ALL sessions (OOM risk) - filter by live status.
Files: `primer/storage/postgres.py` DDL, startup recovery in `primer/api/app.py`.
DoD: backend; unit (index present / query plan if feasible); docs (storage);
UI/tools/primectl N/A.

### hotpaths  [scalability]
Replace full scans on hot paths: channel `_find_thread_chat` (scan all chats)
-> CorrelationStore lookup keyed (channel_id, thread_external_id); chat claim
`_find_next_user_message`/`_find_resume_reply` (full message scan per turn) ->
a `next_unprocessed_seq` cursor on the Chat row; MCP routing map rebuilt per
tools/call -> cache keyed on McpExposure.updated_at; paginate the 200-capped
`list_triggers`/`list_subscriptions`/parked-session sweeps/`list_workspace_sessions`;
`index_document` per-chunk embed -> batch (mirror DocumentIngester). DoD:
backend; unit; docs as needed; UI/tools/primectl N/A. NOTE: may conflict with
sessions-filter (sessions.py) and chat-approval-pending - sequence, don't
parallelize, if they touch the same files.

### correlation-bus  [reliability; multi-worker]
(a) CorrelationStore `upsert_session` is a non-atomic read-modify-write with no
DB uniqueness -> double-resume race; add `UNIQUE(channel_id, anchor)` +
`ON CONFLICT`. (b) PostgresEventBus has no LISTEN reconnect; add a reconnect loop
mirroring the scheduler. Files: `primer/channel/correlation.py`,
`primer/bus/postgres.py`, storage DDL. DoD: backend; unit; docs (channels);
UI/tools/primectl N/A.

### xprocess-workspaces  [completeness; non-local deploy]
`SandboxWorkspace.get_session` + backend `list()` are in-memory only; container/
k8s sessions are dropped across the API/worker process split and vanish after
restart. Implement rehydration (mirror LocalWorkspaceBackend) from docker
inspect / k8s statefulsets / disk; implement `DockerAdapter.get_sandbox`. Files:
`primer/workspace/sandbox/*`, `primer/workspace/runtime/*`, k8s backend. DoD:
backend; gated integration tests (need docker/k8s); docs (workspaces);
UI/tools/primectl N/A. Largest task; may need decomposition.

### delivery  [adoption/safety]
README is 1 line; `config.example.yaml` uses a flat `db_*` form AppConfig
silently ignores (operator quietly runs SQLite); no CI. Write a real README;
fix config.example.yaml to the nested `db: {provider, config: {...}}` form (+
docker entrypoint if it mirrors it); add a CI workflow running the narrowed
sweep + doc hygiene + coverage on push/PR. DoD: docs/build; no backend; verify
config parses; tools/primectl/UI N/A.

### maintainability
(a) Replace `inspect.getsource` heuristics in `primer/toolset/internal.py`
(yielding/requires-session detection) with explicit `yields`/`requires_session`
flags on `make_tool` (update all make_tool call sites + the exposure guard).
(b) Extract duplicated `_ok/_err` toolset helpers into `primer/toolset/_helpers.py`.
(c) `Q.where_null`/`where_not_null` build `= NULL` (broken, zero callers) ->
emit `IS NULL`/`IS NOT NULL` (`primer/storage/q.py`). DoD: backend; unit
(exposure flags, q.py); docs if tool-authoring guide exists; UI N/A.

### ui-polish  [UX]
Console renders mock fixture data in production worker/agent views (confine
window.MOCK to the design canvas); add WS exponential-backoff reconnect (reuse
the initialLoadedSeq cursor) so a blip does not permanently kill live streaming;
fix the stale "executor not yet shipped" copy; guard composer-clear on send
success. Files: `ui/components/*`. DoD: frontend; ui_e2e if feasible; docs N/A;
backend/tools/primectl N/A.

### flaky-test  [maintainability/CI]
The narrowed unit sweep intermittently HANGS at ~99% on a single non-hermetic
test making a real external `:443` call (NOT HF Hub - persists under
HF_HUB_OFFLINE=1). Find it (run `-n0 -v` and watch the last-started test, or
grep tests for real network/tiktoken/docling/SentenceTransformer/httpx calls)
and mock/skip the network so the suite is deterministic. DoD: tests only.

### audit-relaxed  [verification]
Review the 3 graph-fatal-path e2e assertions the isolation subagent RELAXED
(t0429, t0432, t0433 in tests/e2e/test_sessions_top_level.py) on the rationale
"graph executor now implemented, completes instead of NotImplementedError".
Confirm the relaxation is not masking a behavior change; tighten if it is.

### env-e2e + final measurement  [merge gate]
After the above land: run real-LLM e2e (test_smk_real_llm, test_smk_knowledge)
with `LMSTUDIO_API_KEY` set + LM Studio up (provider max_concurrency:1; the
llm-timeout fix prevents hangs); k8s backend tests need a live cluster (skip/mark
if none). Then a FULL serial e2e measurement (`-n0`, exclusive) to confirm the
remaining count -> 0 (modulo infra-gated). This is the gate before declaring
"e2e 100%".

## User-submitted tasks
(Append new tasks here as the user submits them; same per-task format.)

### user-1 remove-bug-reporter  [removal]
Remove the in-app bug-reporter from UI + backend. Backend: delete
`primer/api/routers/bugs.py`; remove its import + `include_router` in
`primer/api/app.py` (~1515-1517); remove any bug-report Pydantic model and the
bugs-directory config field (grep config.py); drop from MCP exposure if listed.
UI: delete `ui/components/bug_reporter.jsx` and its mount (the report
button/modal in `ui/components/chrome.jsx` or `ui/app.jsx`); remove any route in
`ui/foundation/router.js`. Docs: delete
`primer/user_docs/features/bug-reporter.md` + its `manifest.yaml` entry; scrub
`docs/agents`/AGENTS.md refs. Leave on-disk bug files (~/.primer/bugs) untouched
(user data). DoD: backend, UI, docs, unit+e2e tests removed/updated,
regressions, primectl (remove any bugs command). CONFLICTS: touches app.jsx +
router.js -> sequence with user-3 (also touches app.jsx/router), do not
parallelize.

### user-2 collection-search-config-ui  [feature exposure + mutability]
The user Collection model ALREADY has `search: CollectionSearch | None` (mmr +
cer) in `primer/model/collection.py` / `primer/model/search.py`; the backend
supports MMR + cross-encoder rerank on user collections - it is just not in the
UI. (a) Backend: ensure the Collection PUT permits updating `search` while
`embedder` + `search_provider_id` remain create-bound/immutable (adjust the PUT
validator if needed); search is a search-only change (no re-index). (b) UI
(`ui/components/knowledge.jsx` create + edit collection dialogs): add an MMR
toggle + `lambda_mult` + `fetch_k`; a cross-encoder toggle with a
CrossEncoderProvider + model picker (mirror the embedder picker) + `top_n`. In
the EDIT dialog, render the embedder provider/model READ-ONLY (locked); allow
editing `search`. DoD: backend (PUT rules), UI (both dialogs), user docs
(knowledge-collections) + agent docs (docs/agents/knowledge.md), unit tests (PUT
search-updatable + embedder-immutable) + UI render test, e2e (create with
mmr+cer; edit to change them; edit rejects embedder change), regressions,
primectl N/A (generic CRUD passes search through). CONFLICTS: touches
knowledge.jsx -> sequence with user-3 (also knowledge.jsx).

### user-3 remove-entity-search-probe  [removal]
Remove the "Entity search probe" = `SearchBenchPage` at `/knowledge/search` (a
search bench over internal agents/graphs/tools). UI: delete the SearchBenchPage
component (in `ui/components/knowledge.jsx` ~1591 and its render block in
`ui/app.jsx` ~737-744); remove the `/knowledge/search` route
(`ui/foundation/router.js:33`); remove the `collection-search` page-key mapping
(`ui/app.jsx`:67) + URL builder (`ui/app.jsx`:374); remove the "Run a search"
button on the IC page (`ui/components/internal-collections.jsx:348`). KEEP the
per-collection "search" icon (user-collection search). Docs/tests: scrub refs;
remove SearchBenchPage tests + ui_e2e refs. No backend change. DoD: UI, docs,
tests, regressions. CONFLICTS: touches knowledge.jsx (with user-2) + app.jsx/
router.js (with user-1) -> sequence after user-1 and user-2.

### user-4 webhook-trigger  [new feature - largest]
New `webhook` trigger type. Decisions (locked): capability-URL + optional HMAC;
fire-and-forget 202; payload = body + metadata (headers/query/method); POST only.
- Model (`primer/model/trigger.py`): add `TriggerKind.WEBHOOK` +
  `WebhookTriggerConfig(kind="webhook")` with a server-minted unguessable
  `token` and optional `hmac_secret: SecretStr | None`; add to the TriggerConfig
  union. Mint token on create; support rotate + set/clear hmac via update.
- Endpoint: public `POST /v1/webhooks/{token}` mounted OUTSIDE the auth
  dependency. Resolve trigger by token (404 if none); 403 if disabled; if
  hmac_secret set, verify `X-Primer-Signature` HMAC-SHA256 over the RAW body
  (401 on mismatch). Build payload {body, filtered headers, query, method};
  fire-and-forget -> 202 + delivery id. Guardrails: body-size cap, per-token
  rate limit, never leak internal errors.
- Firing: reuse the existing trigger->subscription dispatch (the path
  scheduled/delayed triggers use); thread the webhook payload as the fire
  context into each subscriber (agent/graph fresh-session initial input, chat
  message, parked-session resume).
- UI (`ui/components/` triggers page + create dialog): add "webhook" kind; after
  create show the copyable webhook URL + optional HMAC secret field + a rotate
  action.
DoD: backend (model + endpoint + firing + token mgmt); system tools (webhook
trigger creatable + URL readable via generic trigger CRUD; add a tool only if
needed); UI; user docs (triggers) + agent docs (triggers-and-subscriptions) +
dev doc (docs/dev/subsystems/triggers.md); unit tests (model; endpoint fire/
dispatch, 404 bad token, 403 disabled, HMAC pass/fail, payload mapping); e2e
(create webhook trigger + subscription -> POST webhook -> subscriber invoked);
regressions; primectl (add a path if a dedicated webhook retrieval is warranted).
Mostly independent of user-1/2/3 (own files); can run in parallel with them.
