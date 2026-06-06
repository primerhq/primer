# Findings from the SMK e2e harness (untracked)

Bugs the smoke-test harness surfaced while implementing the SMK suite. Kept here
(untracked, alongside the test docs) so they are not lost.

## FIXED

### F1. Session claim eligibility referenced a non-existent column
- File: `primer/claim/adapters/sessions.py`
- Was `e.parked_status IS NULL` (top-level column); entities are stored as JSONB
  `data`. On Postgres this raised `UndefinedColumnError` in the worker claim
  loop, so no session ever executed in distributed mode.
- Fix: `e.data->>'parked_status' IS NULL` (matches chat/harness/trigger
  adapters). Commit + regression test in `tests/claim/test_session_adapter.py`.

### F2. Claim adapter entity-table names were pluralised + lazy-created (bug #2)
- Files: `primer/claim/adapters/{chats,harnesses,triggers}.py`,
  `primer/claim/postgres.py`
- The chat/harness/trigger adapters used `chats`/`harnesses`/`triggers` as
  `entity_table`, but `_table_name_for` maps those models to singular
  `chat`/`harness`/`trigger`. The claim_due UNION query JOINed tables that never
  match storage; and those tables are created lazily on first write, so on a
  fresh Postgres DB the JOIN target was missing and the whole query failed with
  `UndefinedTableError`, blocking all session/chat/harness/trigger execution.
- Fix: corrected the names; `PostgresClaimEngine` now ensures each adapter's
  entity table exists (standard JSONB shape) on first claim. Regression:
  `tests/claim/test_postgres_engine.py::test_claim_due_on_fresh_schema_ensures_entity_tables`.
- Verified end to end: agent + graph SMK tests now pass on Postgres (was 0,
  now 27/31).

## OPEN (new, distinct from bug #2)

### F3. System-tool entity creation under Postgres + auto_bootstrap loops/compacts
- Symptom: an agent that calls a system tool to create an entity
  (`system__create_agent`, or `system__call_tool`->`create_agent`) on the
  Postgres bringup server (auto_bootstrap on, ~102 system tools offered) does
  NOT reliably leave the entity visible afterward. The agent loops: 23x
  "AgentExecutor: compaction fired" + 13x `409` conflicts in a single run, plus a
  `RuntimeError: Response content longer than Content-Length` and a transient
  `NotFoundError: Toolset 'search' does not exist`.
- Likely chain: the large system-tool result inflates context -> compaction
  fires -> the tool-result message is reshaped/dropped -> the model re-emits the
  same create -> 409 on retry -> loop. Whether the first create commits is
  unclear (the test reads 404 despite 409s on retries).
- Scope: appears only on the Postgres server with the full system toolset +
  compaction; the same tests pass on the sqlite hermetic server (smaller offered
  set, no compaction). Affected SMK tests: TRC-04, TRC-06 (and the earlier
  AGT-03/WSP variants, since reworked to be backend-agnostic).
- Suggested follow-up: confirm whether compaction drops tool-result parts (and
  whether system-tool writes commit independently of the agent turn), and add a
  compaction-preserves-tool-results guard. Tracking separately from bug #2.

### F4. (latent) Empty-adapter claim query is not type-inferable on Postgres
- `tests/claim/test_postgres_engine.py` tests that build `PostgresClaimEngine`
  with `adapters={}` fail with `IndeterminateDatatypeError: could not determine
  data type of parameter $1` when run against real Postgres. The degenerate
  `WHERE FALSE` query leaves `$1` (max_count) untyped. Production always has the
  four adapters, so this is test-only; pre-existing, surfaced when the PG claim
  suite runs against a real instance (it is normally skipped without
  `PRIMER_TEST_POSTGRES_URL`).

## Lane model
- Hermetic lane = sqlite e2e server (in-memory claim engine): all 31 SMK tests
  implemented so far pass.
- Distributed lane = Postgres (bringup): used for DST/LEASE and to validate
  distributed-mode execution. Bug #2 unblocked it.
