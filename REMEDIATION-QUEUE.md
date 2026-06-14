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

---

# Auto-mode execution plan

## Dispatch ledger (coordinator updates on every dispatch + completion)
| task | branch | worktree | agent | status |
|------|--------|----------|-------|--------|
| llm-timeout | (merged) | - | - | merged 40357e2a |
| _all others_ | - | - | - | pending |

## Conflict map (concurrent tasks MUST NOT share a hot file)
- `primer/api/app.py`            : user-1, auth, scale-indexes      (mutually exclusive)
- `ui/app.jsx` + `ui/foundation/router.js` : user-1, user-3
- `ui/components/knowledge.jsx`  : user-2, user-3
- `primer/api/routers/sessions.py` : sessions-filter, hotpaths
- `primer/model/trigger.py` + triggers routes : user-4, hotpaths
- `primer/storage/postgres.py`   : scale-indexes, correlation-bus
- `primer/knowledge/indexing.py` : dim-mismatch, hotpaths
- `primer/channel/correlation.py`: correlation-bus, hotpaths
- `tests/e2e/test_sessions_top_level.py` : sessions-filter, audit-relaxed
- `hotpaths` is the widest hub (sessions/triggers/indexing/correlation/mcp/chat) ->
  run it SOLO (no concurrent task touching those files), or decompose into
  hotpaths-{channel,chat,mcp,paginate,embed}.
- `user-3` depends on user-1 + user-2 (edits app.jsx AND knowledge.jsx) -> dispatch
  only after BOTH merge.

## Priority order (scheduler picks the highest-priority PENDING task whose hot
## files do not overlap any in-flight task and whose deps are merged)
1 failure-isolation  2 user-4 webhook  3 user-2 collection-ui  4 git-timeout
5 chat-approval-pending  6 sessions-filter  7 auth  8 auto-start  9 dim-mismatch
10 user-1 bug-reporter  11 user-3 entity-probe (dep: 3,10)  12 scale-indexes
13 correlation-bus  14 maintainability  15 delivery  16 ui-polish  17 flaky-test
18 hotpaths (solo)  19 xprocess-workspaces  20 audit-relaxed
21 env-e2e + FINAL full serial e2e measurement (LAST, alone - the merge gate)

## Auto-mode coordination protocol
- CONCURRENCY CAP: default 3 in-flight implementer subagents (run_in_background),
  each in its own `feat/<slug>` branch + `../primer-<slug>` worktree branched
  from LATEST main, own `uv sync`. Update the ledger on dispatch.
- PICK RULE: dispatch the next task in priority order whose hot files are
  disjoint from every in-flight task and whose deps are merged, until the cap.
- ON COMPLETION (task-notification): VERIFY before merge - read the diff; assert
  NO em-dash added (`git diff main..feat/<slug> | grep -nP "\x{2014}"` on +
  lines); run the task's blast-radius tests `-n0`; if it claims to green e2e
  tests, run exactly those (`-n0`, after `pkill` any existing `pytest tests/e2e`).
  ALL test runs are SERIALIZED (one at a time, CPU). If good -> `git merge
  --no-ff`, set ledger merged|SHA, move entry to `## Done`, `git worktree remove`
  + delete branch, then pick the next eligible task. If not good -> re-dispatch
  the SAME branch with specific fix notes (max 2 retries) then mark BLOCKED.
- GUARDRAILS: conventional commits, no Co-Authored-By, no em-dash, stage named
  files only, branch off + merge to main, never force-push. Do NOT change product
  behavior to make a test pass; fix the cause or REPORT (see reconciliation
  lesson, review addendum). After merges that touch runtime code, restart the
  dogfood (`uv run primer api --config dogfood/config.yaml`) and confirm
  /v1/health 200. Never run two e2e at once; never `teardown.sh` with volumes.
- STOP / ESCALATE: pause and surface to the user when - a task needs a product/
  contract decision (don't guess), a task is BLOCKED after retries, an
  unresolvable merge conflict, or the env-gated tests need infra (k8s cluster /
  LM Studio). Otherwise keep draining the queue.
- DONE: when the queue drains, run task 21 (env-e2e + full serial e2e) as the
  final gate, then report.

---

# Documentation refactor (concepts -> features merge)

GLOBAL DESIGN (applies to every doc-* task):
- Merge Concepts into Features. New Features section = the ordered slug list
  below. Delete the Concepts section; fold each concept into the intro of its
  Feature page. Fold `troubleshooting` into Reference. Drop `bug-reporter`.
- PAGE TEMPLATE for every Features page: (1) Concept - plain, user-friendly
  language; (2) Configuration - the console form + every knob explained;
  (3) Walkthrough(s) - apply the config step-by-step, UI-centric; (4) What
  happens after - the resulting behavior/outcome. End with `ref:` links.
- Visuals: real-component `embed:` where a fixture fits (reuse the 17 existing +
  the new fixtures doc-foundation pre-creates); ASCII mockups where embedding is
  impractical (e.g. external provider UIs); `mermaid` for concepts/flows. Use
  the getting-started pages (introduction, quickstart) as the reference template.
- Lint: PRIMER_USER_DOCS_STRICT=1; frontmatter (slug,title,section,summary);
  refs + embed ids resolve; NO em-dash U+2014.

## doc-foundation  [FIRST; blocks all doc content pages; dep: user-1 merged]
Restructure `primer/user_docs/manifest.yaml`: delete the `concepts` section;
rebuild the `features` section as the ordered slug list below; add
`troubleshooting` to the `reference` section; remove `bug-reporter`. Create a
valid STUB `.md` (frontmatter + a one-line placeholder body) for every NEW slug
so the manifest + lint pass. Pre-create the new embed fixtures + registry
entries (so content tasks need not touch registry.json/embed-registry.jsx):
embedding-provider (ProvidersPage kind=embedding), ssp (SSPListPage),
cross-encoder-provider (ProvidersPage kind=cross_encoder/rerank), web-search
(WebSearchPage), workspace-provider-create (WorkspacesPage/providers),
channel-provider-create (ChannelProvidersPage), harness (HarnessesPage),
mcp-exposure (MC_McpPage), approvals (ApprovalsPage), toolsets (ToolsetsPage),
collection-create (CollectionsPage). Add a one-page authoring-template note
under `primer/user_docs/_meta/`. DoD: docs structure + stubs + fixtures + lint
green. After this, each content page is its own .md (parallel).

## Content pages (each = one task; PAGE TEMPLATE above; mostly parallel after
## doc-foundation; the only shared files - registry.json/embed-registry.jsx -
## are pre-done by doc-foundation, so content tasks edit ONLY their own .md)

- doc-llm-providers `features/llm-providers`: multiple LLM providers (anthropic,
  openai, openchat, gemini, ollama, openrouter) + concurrency (max_concurrency)
  + request_timeout_seconds. embeds: llm-provider-openrouter.
- doc-agents `features/agents`: creation, tool selection, system prompt,
  temperature, auto-compaction + compaction_prompt, jinja templating in prompts.
  embeds: agents-page, quickstart-agents.
- doc-chats `features/chats`: creation, invocation, compaction, agent switching,
  attaching files. embeds: chat-stream, chat-agent-switch.
- doc-toolsets-system `features/toolsets-system`: each reserved/system toolset
  class (system, search, workspaces, misc, web, harness, trigger) + how to
  explore the tools (list_toolset_tools, search_tools). embeds: toolsets.
- doc-toolsets-mcp `features/toolsets-mcp`: registering MCP toolsets via stdio
  AND http transports. embeds: toolsets/mcp.
- doc-toolsets-approvals `features/toolsets-approvals`: required, policy (Rego),
  and LLM-judge approvals. embeds: approvals. (related code: chat-approval-pending)
- doc-embedding-providers `features/embedding-providers`: all embedding providers
  (huggingface, openai, gemini). embeds: embedding-provider.
- doc-ssp `features/semantic-search-providers`: all SSPs (pgvector,
  pgvectorscale, lance) + halfvec. embeds: ssp.
- doc-collections `features/collections-and-documents` [dep: user-2]: create
  collections incl. MMR + cross-encoder variants; ingest plaintext + file upload;
  search. embeds: collection-list, collection-create.
- doc-cross-encoder `features/cross-encoder-providers`: registering cross-encoder
  providers. embeds: cross-encoder-provider.
- doc-internal-collections `features/internal-collections` [dep: user-3]: config
  + bootstrapping, CDC, exploring ICs under the collections page. embeds:
  internal-collections-enable, collection-list.
- doc-workspace-providers `features/workspace-providers`: local, docker,
  kubernetes + all config variations. embeds: workspace-provider-create.
- doc-workspace-templates `features/workspace-templates`: template creation,
  recipe examples covering features per provider, ending with workspace creation.
  embeds: workspace-template-form, workspaces.
- doc-sessions `features/sessions`: creating sessions, statuses, turns. embeds:
  sessions-list, session-detail.
- doc-workspace-toolset `features/workspace-toolset`: the workspaces toolset
  (read/write/edit/glob/grep/ls/exec) + use. embeds/mockup + mermaid.
- doc-yielding-tools `features/yielding-tools`: yielding tools (ask_user,
  subscribe_to_trigger, watch_files) + an example in a session (park/resume).
  embeds: session-detail + mermaid.
- doc-graphs `features/graphs`: graph concept, creation, execution, the canvas.
  embeds: graph-canvas.
- doc-graph-nodes `features/graph-node-types`: every node kind (begin, end,
  agent, graph, fan_out, fan_in, tool_call) - role + config each. mermaid per shape.
- doc-graph-templating `features/graph-templating` [VERY DETAILED]: node input
  templating; ALL template data available per node type (nodes.<id>.text/.parsed,
  iteration, fanout_index/fanout_item, etc.); worked examples. code-tabs + mermaid.
- doc-web-search `features/web-search-providers`: all providers (duckduckgo,
  tavily, firecrawl, exa) + the aggregated fallback chain. embeds: web-search.
- doc-channel-providers `features/channel-providers`: each provider + the
  provider-END setup in detail (telegram BotFather, slack app config, discord
  application) as mockups; single-type vs multi-type channels. embeds:
  channel-provider-create + ASCII mockups of external UIs.
- doc-channels `features/channels`: channel registration, chat enablement,
  per-provider interactions (/agent, /new). embeds: channels + mermaid.
- doc-channel-association `features/channel-workspace-association`: associating a
  channel with a workspace; example interaction via yielding tools; per-provider
  specifics. embeds: workspaces (association) + mermaid round-trip.
- doc-triggers `features/triggers` [dep: user-4]: scheduled, cron, AND webhook
  triggers; subscription via yielding tools (subscribe_to_trigger). embeds:
  trigger-create.
- doc-harnesses `features/harnesses`: harness structure, templating, inbound +
  outbound harnesses, syncing. embeds: harness.
- doc-mcp-server `features/mcp-server`: API tokens + MCP server endpoint
  enablement (McpExposure allowlist, scopes). embeds: api-token-create, mcp-exposure.
- doc-workers `features/workers`: backend architecture - how workers + leases
  work in detail (claim machine, heartbeat, park/resume). embeds: workers-stats +
  mermaid; ref docs/dev/architecture/{worker-system,claim-machine}.

## Conflict map + priority additions for docs
- doc-foundation: depends on user-1 (both touch manifest + bug-reporter doc);
  run user-1 first, then doc-foundation. doc-foundation BLOCKS all doc content
  pages (they need its stubs + pre-created fixtures + manifest slots).
- After doc-foundation merges, content pages are pairwise file-disjoint (each its
  own features/<slug>.md; registry pre-done) -> safe to run at high concurrency,
  ideal filler to keep the cap saturated alongside code tasks.
- Cross-deps: doc-collections after user-2; doc-triggers after user-4;
  doc-internal-collections after user-3.
- Priority: slot doc-foundation right after user-1; let content pages fill
  parallel slots throughout (low-risk, disjoint). The dep-gated pages
  (collections/triggers/internal-collections) wait for their code dep to merge.

---

# Open-source launch (approved strategy)
Positioning: "Primer - orchestrate fleets of small, context-optimized agents
that rival a single frontier model; run capable agents on your own hardware."
Balanced goals (awareness + adoption + design-partners), all audiences,
Apache-2.0, ~7-day runway. Lean into the honest thesis-under-test framing.

- marketing-strategy `docs/OPEN-SOURCE-LAUNCH-STRATEGY.md`: write the full doc -
  positioning; 7-day phase plan (repo readiness -> assets/content -> soft
  pre-launch -> coordinated launch day -> sustain); per-asset claude-designer
  briefs (logo/wordmark, OG card, layered architecture diagram, 60-90s demo
  video + GIFs); channel playbook (Show HN timing, r/LocalLLaMA + r/selfhosted,
  X thread, Product Hunt, Lobste.rs, dev.to); the metric dashboard (HN
  rank/points, stars + stars/day, X impressions, Reddit upvotes, GH referral
  traffic; quickstart completions, Docker pulls, opt-in active instances; Discord
  members, issues/PRs, design-partner signups); risks + mitigations; and copy
  DRAFTS for Show HN, the launch blog, Reddit posts, the X thread. DoD: docs only.
- oss-prep (SUPERSEDES `delivery`): Apache-2.0 LICENSE + source headers; real
  README (hero, demo GIF, quickstart, architecture, badges); CODE_OF_CONDUCT.md;
  SECURITY.md; `.github/` (issue + PR templates, FUNDING, a CI workflow running
  the narrowed sweep + doc hygiene + coverage); fix `config.example.yaml` to the
  nested db form; scan git history + tree for secrets / internal-only refs.
  Gate: a newcomer can clone -> run in <10 min. DoD: docs/build/CI; verify config
  parses; no backend behavior change.
- launch-assets [dep: marketing-strategy + oss-prep]: execute the claude-designer
  briefs from the strategy doc (produce the logo, OG/social card, architecture
  diagram, demo GIF/video) and finalize the launch copy. DoD: assets + copy
  committed under e.g. `docs/launch/` or `assets/`.

Priority: marketing-strategy can run early (docs-only, independent); oss-prep
early (independent, supersedes `delivery` - drop `delivery`); launch-assets after
both. All three are file-disjoint from the code/doc tasks.
