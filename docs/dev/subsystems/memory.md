# Memory

## 1. Purpose

Agent memory is what an agent learns between turns and the notes a turn
carries into its first model call. It gives each agent one row of owner-set
settings, a private collection of learned entries, a job queue that runs
the learning (reflection) and the upkeep (curation), and a console surface
to read and steer it. Memory is off until the agent's owner turns it on.

## 2. Conceptual model

- **Settings** - one `AgentMemory` row per agent (the row id IS the agent
  id, `primer/model/memory.py`). The owner edits a fixed field list
  (`EDITABLE`, `primer/memory/settings.py`); the rest of the row (the day's
  reflection count, the curator's last run, the topics suggestion) is the
  system's.
- **Entries** - a learned entry is a document `<folder>/<slug>` in the
  agent's private collection `mem-<agent id>` (`memory_collection_id`,
  `primer/memory/collection.py:35`); `folder` is one of `active`,
  `candidates`, `archived` (the three folders are the entry's status,
  `primer/memory/store.py:40`). An entry's title is its name and its body
  is three item lists - `steps`, `pitfalls`, `example`
  (`MemoryEntry`, `primer/memory/store.py:47`).
- **Jobs** - a `MemoryJob` row (one of the kinds `reflect`, `curate`,
  `topics`) plus its lease on the `memory` claim kind
  (`ClaimKind.MEMORY` -> `MemoryClaimAdapter` over the `memoryjob` table,
  `primer/claim/factory.py:79-81`, `primer/claim/adapters/memory.py`).
  Every completed turn records a reflection deadline of `idle_minutes` later, and an ended session makes it
  due at once (`primer/memory/jobs.py`).

## 3. Architecture patterns implemented

The claim machine (the jobs are claimed and released like every other kind,
with the engine's field-scoped lease writes); storage (the settings and job
rows are ordinary JSONB storage rows, the entries are ordinary documents);
the access levels (the routes are full-user tier, every write is
owner-only, `primer/api/routers/agent_memory.py` `_require_owner`, 403
`memory_owner_only`); and RFC7807 refusals. Writes are field-scoped and
fenced (`primer/memory/settings.py` `save_settings` guards on `enabled`,
the field an on/off transition is judged from, so a concurrent budget write
is never put back).

## 4. Code layout

- `primer/memory/` - `settings.py` (read/change the settings row, the
  on-off transitions), `collection.py` (provision/delete the `mem-<agent
  id>` collection and purge its entries), `store.py` (the entry store over
  documents: add/patch/credit/move by name), `jobs.py` (the queue: the
  hooks that enqueue, the daily budget, the gates every job passes,
  `MAX_ATTEMPTS`), `reflect.py` (the reflection run: the turn-diff input,
  the structured model call - at most three deltas, every delta scrubbed -
  the per-delta writes, the
  `_mark_reflected` marker), `curate.py` (the daily curator: merges near
  duplicates, archives the stale, snapshots before its first change, never
  deletes), `prompts.py`, `scrub.py` (the personal-data scrub the
  reflection applies before a write).
- `primer/api/routers/agent_memory.py` - the REST surface (section 8).
- `primer/claim/adapters/memory.py` - the `memory` claim kind.
- `primer/worker/engine_handlers.py` - the worker handler: a job that
  raises during model work is retired. If settlement itself fails, recovery
  reads the last committed intent before scheduling work again.
- `ui/components/agent-memory.jsx` - the console surface (section 8).

## 5. Data model

`AgentMemory` carries the owner's fields - `enabled` (default off),
`promotion` (`auto`/`approve`), `teachers` (`owner`/`everyone`),
`idle_minutes`, `auto_inject`, `inject_top_k`, `inject_min_score`,
`max_index_entries`, `max_active`, `archive_after_days`,
`daily_reflections`, `search` (a `CollectionSearchConfig`, `None` for the
install default), `capture_topics`, `do_not_capture`, `reflection_prompt` -
plus the system's fields (`topics_edited`, `topics_suggestion`,
`reflections_day`, `reflections_count`, `last_curated_at`), in
`primer/model/memory.py`. `MemoryJob` is the claim entity (kind, agent,
session, turn bounds). `MemoryEntry` (section 2) is the worker's view of a
document; the routes and tools show it through `as_dict`.

## 6. Lifecycle

Turning memory on provisions the collection (a search the providers cannot
serve refuses the whole change) and queues the topics job (now) and the
curator (in 24 hours, `FIRST_CURATION`); turning it off keeps every entry;
deleting the agent removes everything (`primer/memory/settings.py`).
Reflection: the completed-turn hook persists the debounce deadline and an
ended session makes the job due at once; the worker then runs the gates in
order (job row gone - drop; agent gone - delete the job and drop; memory
disabled - drop; a week-old job - delete and drop; over the daily budget -
requeue to the next UTC midnight; a session too small to learn from -
mark reflected, no model call) and, past them, calls the model with the
turns since the last reflection, applies the structured answer delta by
delta (`add_candidate` is a no-op when the name already names an entry in
any folder, `store.py` `add_candidate`; a patch applies its edits as a new
revision; a credit tallies), and only then marks the job reflected
(`_mark_reflected`, `primer/memory/reflect.py`). A reflection runs on the
agent's own model profile, never the session's override; an answer that
fails validation or carries a scrub hit is dropped whole (one skip event, no
retry). A transient model failure
retries from settlement time using the worker's capped exponential backoff
(`base_backoff_seconds`, `max_backoff_seconds`) and is given up after `MAX_ATTEMPTS`
(`primer/memory/jobs.py:32`) with its skip record. Curation runs on its own
job at most once a day (`CURATE_EVERY`): it merges near-duplicates, archives
entries past `archive_after_days`, keeps the active set under its cap, and
writes a JSON snapshot of every entry before its first change - a change is a
move to `archived/` (a merge leaves a `merged_into` pointer), a pass that
finds nothing writes nothing, and the snapshots live outside the three
folders (an agent's searches never reach them).

## 7. Persistence

The settings row (one per agent), the `memoryjob` rows, and the entries -
ordinary documents of `mem-<agent id>`, whose titles are the entry names
and whose search index is what `auto_inject` and the console's search read.
A new scheduling request commits its absolute `requested_due_at`, target and
expiry in the same job write. The stable job id still serializes one session's
reflection (or an agent's topics/curation). Creation assigns a random
`incarnation_id`. Every request assigns a fresh `occurrence_id`, even for the
same target and deadline. Completion clears the requested deadline, retaining
the reflection cursor. A retry, budget delay or next daily pass writes a new
occurrence and deadline before lease release. Retry count and diagnostic text
survive loss of an in-process queue.

The claim lease projects this request. After commit, `arm_current` locks and
re-reads the job in a **new** model transaction. A delayed enqueuer or recovery
scan can only project the latest committed occurrence. In PostgreSQL the engine
writes on that unit's checked-out connection. In single-process mode it sets
the in-memory entry while holding the job lock. Repeating a projection leaves
the holder, deadline and attempt state alone. Neither mode assumes an atomic
commit across a model transaction and process memory.

The elected memory schedule reconciler starts with the runtime and cycles
through bounded keyset pages of pending jobs. It retries a failed arm on the
next pass. A crash after job commit but before notification, or a process
restart with an empty in-memory queue, preserves the deadline. An unset
deadline on a managed row means idle. The lease records the scheduled occurrence
and a fresh random receipt for each acquisition, even when the same worker
reclaims the same occurrence after expiry. Heartbeat, release and job writes
from a worker require its current receipt. Renewal keeps the old worker's slot
occupied until hand-back or expiry. A late release can return that slot without
applying its own drop, delay or attempt data to the replacement. A duplicate
release after the next claim has no authority over the new holder.

Topics completion, expiry and missing-agent cleanup still compare the entire
decoded snapshot under a model row lock, then use guarded deletion. This keeps
legacy timestamp spellings readable and distinguishes managed jobs recreated
with otherwise identical data. Progress and expiry updates also match the
job occurrence. The reconciler prunes unheld, orphaned leases after a pass.

Legacy rows and leases have null occurrence fields. Their existing lease
deadline remains authoritative until a fresh request or a subsequent retry or
daily pass supplies a known deadline. Recovery cannot infer a lost deadline
from a legacy row's creation or expiry time. The pre-intent curator fallback
remains for legacy topics only. Use a coordinated cutover. Quiesce enqueue traffic and drain and stop every
old API, worker and reconciler before enabling new writers. Old writers ignore
the receipt and can delete a renewed lease. PostgreSQL preserves existing lease
deadlines across this cutover. Drain legacy in-memory work first: an old
process's lost volatile queue has no recoverable due time. Downgrade requires
draining managed schedules and holders first. Model calls, budget consumption
and changes to memory entries can repeat if an execution loses its lease or
dies before settlement. PostgreSQL initialization adds nullable columns to existing lease tables and
preserves old job documents.

## 8. Public surfaces

REST (`primer/api/routers/agent_memory.py`, user tier):
`GET`/`PUT /v1/agents/{agent_id}/memory/settings`,
`GET /v1/agents/{agent_id}/memory/entries` (by status),
`GET`/`PATCH /v1/agents/{agent_id}/memory/entries/{name}`,
`POST .../entries/{name}/approve`, `POST .../entries/{name}/archive`,
`POST /v1/agents/{agent_id}/memory/purge`, and
`GET /v1/agents/{agent_id}/memory/activity` (the owner's feed).
Console (`ui/components/agent-memory.jsx`): `AG_MemorySettings`, the
owner's switches in the agent editor (the `agent.memory` verb opens the
fold), and `AG_MemoryPanel` - what the agent has learned, by folder, the
candidates inbox and the activity feed; everything below owner is disabled
or hidden, and the server refuses it too. Turn input: with `auto_inject`
on, the workspace executor's `_with_turn_messages`
(`primer/agent/workspace_executor.py:179`, over the seam
`primer/agent/base.py:477-478`) prepends the notes in front of the user's
message, prompt-only.

## 9. Internal contracts

A job is claimable for as long as its row exists (the adapter's
`eligibility_sql` is `TRUE`; a missing row is not eligible because the
claim query inner-joins the entity table,
`primer/claim/adapters/memory.py`). The gates hold in the worker, not the
claim engine. A job that raises is dropped, never looped (section 4). The
`_mark_reflected` marker is the at-most-once boundary of a reflection: a
crash between the per-delta writes and the marker re-runs the LLM call on
the next trigger, and the name-keyed `add_candidate` suppresses duplicate
entries. The credit path is replay-safe too (EXEC2-03, sweep ledger
G15-iter7): each apply records its delta key under the run's key in the
entry's `meta['applied_runs']` in the same statement as the apply
(`primer/memory/store.py`), and an already-recorded delta is skipped, so a
replay of a crashed run is a no-op at the storage level where before it
double-credited by one.

### Monotonic target merges

The enqueue helper owns a one-model `locking_transaction(models=(MemoryJob,))`,
reads the current row with `get_for_update`, decodes it, and patches only the
maximum of current/requested target, expiry and scheduling intent on the same token. It does not
compare reconstructed canonical dates or integers against historical raw JSON.
Every representation readable by the model remains readable; untouched identity
fields retain their stored representation. Invalid stored rows still fail.

This helper requires the delivered SQLite/PostgreSQL model-locking capabilities
(and the test fake). Extension providers/wrappers must implement/forward both
provider locking and model locked reads. Unsupported or nested ordinary/locking
units refuse visibly before mutation; there is no unlocked fallback. Projection begins only after this producer transaction exits.

Only the exact original create `ConflictError` may cause one fresh recovery unit,
after a confirmed body rollback and complete owner/pool cleanup. The recovery
reads a current winner under lock and cannot recreate a vanished winner. New
cancellation requests during cleanup forbid recovery, even when cleanup preserves
the original error. Read/patch, commit, pool-reset or uncertain cleanup errors
propagate without retry. Cancelled/failed commit can already be durable and must
not be reported as a rollback or automatically replayed. PostgreSQL does not gap
lock a missing row, so two initial creators can genuinely collide.

A foreign owner or vanished create winner leaves both the job and lease
alone. Readable creation, kind/session identity and reflected progress are
preserved on a target merge. Cooperating writers are serialized by the job
row lock. PostgreSQL does not gap-lock a missing row. Request order is the
order of accepted job writes: a completed turn requests its idle deadline,
and ending the session requests immediate work. The expiry is still the
caller's time plus seven days.

These guarantees start at the committed job request. Session hooks remain
best effort, and changing agent settings and creating its initial jobs are
separate domain operations. Model calls, memory-document effects and job
completion remain at-least-once across process loss or lease expiry. The
occurrence fence protects the queue, progress marker and cleanup across a
renewal; it cannot retract an external side effect already in flight.

## 10. Testing patterns

`tests/claim/test_memory_intent.py` exercises projection, restart, release and
recreation on both claim engines. Its required PostgreSQL cases include process
exit immediately after intent commit, both lock-queue orders of release
versus replacement arm, same-worker reacquisition and migration of old leases. The memory settings API tests verify the exact two
deadlines from an accepted owner request on a replica without an engine.

`tests/memory/test_job_target.py` covers controlled SQLite/fake/PostgreSQL
locked target races, current identity/disappearance, expiry and hook deadlines.
`test_job_target_locking.py` adds legacy representations, actual independent
writer/create contention and rollback/commit/cancellation boundaries.
Their marked native cases run in separate required PostgreSQL lane steps;
collection or a local fake pass is not native acceptance.
`tests/storage/test_memory_target_cleanup_interop.py` composes both production
helpers across target commit/rollback, stale expiry cleanup and readable legacy
representations; its marked PostgreSQL cases run in the existing storage lane.

`tests/memory/` covers the settings transitions and the collection
(`test_jobs.py`, `test_collection.py`), the entry store and body
(`test_entry_body.py`, `test_runtime.py`), the reflection run and the
injection (`test_reflect.py`, `test_auto_inject.py`), the curation
(`test_curate.py`), the scrub (`test_scrub.py` over
`scrub_corpus.txt`), and a real session log as input
(`test_real_session_log.py`). The claim-kind wiring rides the
`tests/claim` and `tests/worker` suites; the routes ride
`tests/api` (owner-only refusals included).
`tests/storage/test_memory_job_cleanup.py` exercises the actual cleanup paths
against the fake, private SQLite and gated PostgreSQL, including stale
snapshots, legacy timestamps, cancellation, refused deletion and peer writers.

## 11. Bounded reflection input

`primer/memory/reflection_input.py` budgets the entire request, including the
system instruction, context, transcript, JSON schema and output allowance.
Its shared CJK-aware token estimate is not a native count: input is limited to
30,000 estimated tokens and 80% of the profile window after the output reserve.
The agent's own resolved profile supplies the window (the minimum member window
for an aggregate). Oversized optional index rows and related entries are removed
whole and counted; capture/exclusion policy and owner instructions are never cut.

Short evidence uses one tool-free structured call. Longer evidence is split into
at most four chronological chunks, preserving every text fragment, tool identity,
error flag and source offset, including the middle of a long individual result.
Each chunk receives one independent tool-free summary call. Source references and
summary sizes are validated, then one final reflection reads the lossy summaries.
No original logs, conversation compaction markers or memory entries are rewritten
by preprocessing. Summaries cannot expand deterministic credit eligibility.
Nontext attachments are identified but their contents are not inspected.

Every call has an output request limit, a 64 KiB collected-text/ reasoning-text
ceiling and an event-count ceiling; summary text has a smaller per-plan byte cap.
After profile/log loading, context preparation and model calls share a 120-second deadline. Cancellation joins provider
cleanup, even after repeated waiter cancellation; an adapter that cannot finish
cleanup can delay that join. There is no detached background model task. Profile
settings are passed without an automatic setting-refusal retry in this path;
aggregate adapters retain their own member failover. Bounds describe adapter
invocations and collected output, not arbitrary provider network frames or costs.

An uncovered source range, oversized fixed policy, invalid summary, output limit,
context rejection or changed job/settings yields a fixed skip reason without
advancing `reflected_turn` or applying deltas. Current occurrence settlement retires
that attempt; a later completed turn can enqueue again. Preserved input is not a
promise that an ended, permanently oversized session will eventually be learned.
Transport errors keep the existing bounded durable retries. Invalid *final*
reflection verdicts retain the existing terminal watermark behavior. Summaries
are ephemeral, so a retry recomputes them; existing per-delta replay limits remain.
The range check also refuses a readable log with fewer turn inputs than the job
requests (`input_incomplete`), including at the triviality gate for eligible
multi-turn jobs. The existing fewer-than-two-new-turns policy remains unchanged.

The daily reflection count still counts attempts, not preprocessing requests. A
long attempt can issue four summary calls and one final call; retries consume
another attempt. Activity reports direct/compacted mode, call count, estimated
input and budget, or a safe skip reason. It stores no summary or raw model error.
The existing owner-only activity route and generated `agent_memory__memory_activity`
tool carry these fields; the Memory activity tab explains skipped learning.

This is independent of measurement-gated native token calibration on live turns.
It does not add a strict global token-spend budget, stream the whole source log
from disk, atomically couple entry writes to claims, or guarantee summary fidelity.

## 12. Historical decisions

Memory arrived as a task-based feature (the package's docstrings name the
tasks): the entries live in the knowledge subsystem's documents (one
collection per agent) rather than their own table, the jobs ride the claim
engine rather than a scheduler table, and the reflection's writes are
per-delta with a final marker rather than one transactional apply (the
crash-window trade-off is section 9).
