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
  Reflection is debounced by the lease: every completed turn re-arms
  `next_attempt_at` to `idle_minutes` later, and an ended session makes it
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
  raises is dropped (its lease released with `drop_lease=True`), never
  retried in a loop; its trigger re-enqueues it.
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
Reflection: the completed-turn hook re-arms the lease's debounce and an
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
retries on the claim engine's backoff and is given up after `MAX_ATTEMPTS`
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
The lease's `next_attempt_at` is the debounce; nothing else is written back
by the claim engine (`MemoryClaimAdapter.on_release` is a no-op).

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
entries (a credit can double-count by one - known, see the sweep ledger
G15-iter7 / EXEC-2 EXEC2-03).

## 10. Testing patterns

`tests/memory/` covers the settings transitions and the collection
(`test_jobs.py`, `test_collection.py`), the entry store and body
(`test_entry_body.py`, `test_runtime.py`), the reflection run and the
injection (`test_reflect.py`, `test_auto_inject.py`), the curation
(`test_curate.py`), the scrub (`test_scrub.py` over
`scrub_corpus.txt`), and a real session log as input
(`test_real_session_log.py`). The claim-kind wiring rides the
`tests/claim` and `tests/worker` suites; the routes ride
`tests/api` (owner-only refusals included).

## 11. Historical decisions

Memory arrived as a task-based feature (the package's docstrings name the
tasks): the entries live in the knowledge subsystem's documents (one
collection per agent) rather than their own table, the jobs ride the claim
engine rather than a scheduler table, and the reflection's writes are
per-delta with a final marker rather than one transactional apply (the
crash-window trade-off is section 9).
