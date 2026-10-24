"""Each claim adapter's two eligibility rules agree row by row: ``eligibility_sql`` (the Postgres claim query) and ``is_eligible`` (the
in-memory engine) (ticket 01a16e79).

One table of entities covers every column each clause reads, with the boundary rows: ``parked_status`` null, ``resumable`` and
``parked`` (a parked session stays RUNNING), ``pending_operation`` set and cleared (a finished operation, a failed one), a trigger's
``next_fire_at`` null, before, EQUAL TO and one microsecond after ``now``, disabled, and an event kind; a tool-call task in each state
(``failed`` is what a rejected retry release leaves). Three rows are edited in the stored document after they are written, the way a
hand edit or an older build leaves them (review of #928, round 2): a trigger without ``enabled`` and a task without ``state`` (the SQL
reads NULL, so not eligible, where the model would default them to True and ``queued``), and a trigger whose ``next_fire_at`` has no
zone (read as UTC on both sides; the SQL half pins the session to UTC, as on the default Postgres image). The table is built from
``now`` so the SQL half can use the transaction's own ``now()``, which Postgres holds fixed for the whole transaction.

* The Python half runs everywhere: ``is_eligible`` over the rows stored in SQLite must give the expected column.
* The SQL half runs in the CI Postgres lane only (``tests/pg_gate.py``): the rows are written through the Postgres storage inside one
  transaction (rolled back), ``eligibility_sql`` selects from them at that transaction's ``now()``, and its answer must equal
  ``is_eligible``'s over the same rows at the same instant, row by row.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from pydantic import BaseModel

from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.int.claim import ClaimAdapter, ClaimKind
from primer.model.harness import Harness, HarnessOperation, HarnessStatus
from primer.model.provider import PoolConfig, PostgresConfig, SqliteConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.trigger import ChannelTriggerConfig, DelayedTriggerConfig, ScheduledTriggerConfig, Trigger
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url

_WHAT = "the claim-eligibility parity tests"
NOW = datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Row:
    label: str
    kind: ClaimKind
    entity: BaseModel
    eligible: bool
    drop_key: str | None = None                         # a top-level key the stored document is written without
    naive_key: tuple[str, datetime] | None = None       # a top-level time the stored document holds without a zone


def _rows(now: datetime, prefix: str) -> list[_Row]:
    def sid(name: str) -> str:
        return f"{prefix}-{name}"

    def session(name: str, **extra) -> WorkspaceSession:
        return WorkspaceSession(
            id=sid(name), workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), created_at=now, turn_no=1, **extra,
        )

    def harness(name: str, *, pending: HarnessOperation | None, status: HarnessStatus) -> Harness:
        return Harness(id=sid(name), slug=f"h{uuid.uuid4().hex[:8]}", name="h", status=status, pending_operation=pending, created_at=now)

    def trigger(name: str, config, *, enabled: bool = True, next_fire_at: datetime | None) -> Trigger:
        return Trigger(
            id=sid(name), slug=f"t-{name}-{uuid.uuid4().hex[:8]}", name="t", config=config, enabled=enabled, next_fire_at=next_fire_at,
            created_at=now - timedelta(days=1),
        )

    def task(name: str, state: ToolCallTaskState) -> ToolCallTask:
        return ToolCallTask(
            id=sid(name), session_id="sess-1", turn_no=0, tool_name="workspace__write", state=state, record_seq=1, created_at=now,
        )

    daily = ScheduledTriggerConfig(cron="0 9 * * *", timezone="UTC")
    tick = timedelta(microseconds=1)
    utc_wall = now.astimezone(timezone.utc).replace(tzinfo=None)
    return [
        _Row("session never parked", ClaimKind.SESSION, session("s-free", status=SessionStatus.RUNNING), True),
        _Row("session resumable", ClaimKind.SESSION, session("s-resumable", status=SessionStatus.RUNNING, parked_status="resumable",
                                                             parked_at=now), True),
        _Row("session parked, RUNNING", ClaimKind.SESSION, session("s-parked", status=SessionStatus.RUNNING, parked_status="parked",
                                                                   parked_at=now), False),
        _Row("session parked, WAITING", ClaimKind.SESSION, session("s-parked-w", status=SessionStatus.WAITING, parked_status="parked",
                                                                   parked_at=now), False),
        _Row("session ended, no park", ClaimKind.SESSION, session("s-ended", status=SessionStatus.ENDED), True),
        _Row("harness with an operation", ClaimKind.HARNESS, harness("h-pending", pending=HarnessOperation.SYNC, status=HarnessStatus.READY),
             True),
        _Row("harness operation finished", ClaimKind.HARNESS, harness("h-done", pending=None, status=HarnessStatus.INSTALLED), False),
        _Row("harness operation failed", ClaimKind.HARNESS, harness("h-failed", pending=None, status=HarnessStatus.ERROR), False),
        _Row("scheduled, due a second ago", ClaimKind.TRIGGER, trigger("t-past", daily, next_fire_at=now - timedelta(seconds=1)), True),
        _Row("scheduled, due exactly now", ClaimKind.TRIGGER, trigger("t-now", daily, next_fire_at=now), True),
        _Row("scheduled, due a microsecond after now", ClaimKind.TRIGGER, trigger("t-next", daily, next_fire_at=now + tick), False),
        _Row("scheduled, no next tick", ClaimKind.TRIGGER, trigger("t-none", daily, next_fire_at=None), False),
        _Row("scheduled, disabled", ClaimKind.TRIGGER,
             trigger("t-off", daily, enabled=False, next_fire_at=now - timedelta(seconds=1)), False),
        _Row("delayed, due exactly now", ClaimKind.TRIGGER, trigger("t-delayed", DelayedTriggerConfig(fire_at=now), next_fire_at=now), True),
        _Row("delayed, fired", ClaimKind.TRIGGER,
             trigger("t-fired", DelayedTriggerConfig(fire_at=now), enabled=False, next_fire_at=None), False),
        _Row("channel (event kind) with a past next_fire_at", ClaimKind.TRIGGER,
             trigger("t-channel", ChannelTriggerConfig(provider_id="p1"), next_fire_at=now - timedelta(seconds=1)), False),
        _Row("tool call queued", ClaimKind.TOOL_CALL, task("c-queued", ToolCallTaskState.QUEUED), True),
        _Row("tool call running", ClaimKind.TOOL_CALL, task("c-running", ToolCallTaskState.RUNNING), True),
        _Row("tool call gated", ClaimKind.TOOL_CALL, task("c-gated", ToolCallTaskState.GATED), False),
        _Row("tool call done", ClaimKind.TOOL_CALL, task("c-done", ToolCallTaskState.DONE), False),
        _Row("tool call failed (a rejected retry release)", ClaimKind.TOOL_CALL, task("c-failed", ToolCallTaskState.FAILED), False),
        _Row("scheduled, due a second ago, stored without enabled", ClaimKind.TRIGGER,
             trigger("t-no-enabled", daily, next_fire_at=now - timedelta(seconds=1)), False, drop_key="enabled"),
        _Row("tool call stored without state", ClaimKind.TOOL_CALL, task("c-no-state", ToolCallTaskState.QUEUED), False,
             drop_key="state"),
        _Row("scheduled, stored naive, a second ago in UTC", ClaimKind.TRIGGER,
             trigger("t-naive-past", daily, next_fire_at=now - timedelta(seconds=1)), True,
             naive_key=("next_fire_at", utc_wall - timedelta(seconds=1))),
        _Row("scheduled, stored naive, a second after now in UTC", ClaimKind.TRIGGER,
             trigger("t-naive-next", daily, next_fire_at=now + timedelta(seconds=1)), False,
             naive_key=("next_fire_at", utc_wall + timedelta(seconds=1))),
    ]


_MODELS = {
    ClaimKind.SESSION: WorkspaceSession, ClaimKind.HARNESS: Harness, ClaimKind.TRIGGER: Trigger, ClaimKind.TOOL_CALL: ToolCallTask,
}


def _adapters(provider) -> dict[ClaimKind, ClaimAdapter]:
    return {
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=provider.get_storage(WorkspaceSession)),
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=provider.get_storage(Harness)),
        ClaimKind.TRIGGER: TriggerClaimAdapter(storage=provider.get_storage(Trigger)),
        ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=provider.get_storage(ToolCallTask)),
    }


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "parity.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _edit_sqlite(provider: SqliteStorageProvider, row: _Row) -> None:
    from primer.storage.sqlite import _table_name_for

    table = _table_name_for(_MODELS[row.kind])
    if row.drop_key is not None:
        await provider.connection.execute(
            f'UPDATE "{table}" SET data = json_remove(data, ?) WHERE id = ?', (f"$.{row.drop_key}", row.entity.id),
        )
    if row.naive_key is not None:
        key, value = row.naive_key
        await provider.connection.execute(
            f'UPDATE "{table}" SET data = json_set(data, ?, ?) WHERE id = ?', (f"$.{key}", value.isoformat(), row.entity.id),
        )
    await provider.connection.commit()


async def _python_answers(provider, rows: list[_Row], now: datetime) -> dict[str, bool]:
    """``is_eligible`` for every row, read back from ``provider`` after storing them there (and editing the stored document)."""
    for row in rows:
        await provider.get_storage(_MODELS[row.kind]).create(row.entity)
        await _edit_sqlite(provider, row)
    adapters = _adapters(provider)
    return {row.label: await adapters[row.kind].is_eligible(row.entity.id, now) for row in rows}


def test_the_table_covers_every_kind():
    assert {row.kind for row in _rows(NOW, "p")} == set(ClaimKind)


async def test_python_half_is_eligible_answers_the_expected_column(sqlite_provider):
    rows = _rows(NOW, "py")

    answers = await _python_answers(sqlite_provider, rows, NOW)

    assert answers == {row.label: row.eligible for row in rows}


async def test_python_half_a_missing_entity_is_never_eligible(sqlite_provider):
    """The claim query's inner join: a lease whose entity row is gone is never claimed."""
    adapters = _adapters(sqlite_provider)
    assert {kind: await adapter.is_eligible("missing", NOW) for kind, adapter in adapters.items()} == {kind: False for kind in ClaimKind}


# ---------------------------------------------------------------------------
# The SQL half (CI Postgres lane)
# ---------------------------------------------------------------------------


def _pg_config(url: str) -> PostgresConfig:
    p = urlparse(url)
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=explicit_port(p),
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=parse_qs(p.query).get("schema", ["public"])[0],
        pool=PoolConfig(min_size=1, max_size=4),
    )


@needs_postgres(_WHAT)
async def test_sql_half_eligibility_sql_agrees_with_is_eligible_row_by_row(sqlite_provider):
    from primer.storage.postgres import PostgresStorageProvider, _table_name_for

    pg = PostgresStorageProvider(_pg_config(require_postgres_url(_WHAT)))
    await pg.initialize()
    try:
        async with pg.pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            try:
                # A time without a zone is cast in the session's zone: pin it to UTC, the zone the ruling reads it in.
                await conn.execute("SET LOCAL TimeZone = 'UTC'")
                now = await conn.fetchval("SELECT now()")
                rows = _rows(now, f"parity-{uuid.uuid4().hex[:12]}")
                for row in rows:
                    await pg.get_storage(_MODELS[row.kind]).create(row.entity, conn=conn)
                    table = f'"{pg.schema}"."{_table_name_for(_MODELS[row.kind])}"'
                    if row.drop_key is not None:
                        await conn.execute(f"UPDATE {table} SET data = data - $1::text WHERE id = $2", row.drop_key, row.entity.id)
                    if row.naive_key is not None:
                        key, value = row.naive_key
                        await conn.execute(
                            f"UPDATE {table} SET data = jsonb_set(data, ARRAY[$1::text], to_jsonb($2::text)) WHERE id = $3",
                            key, value.isoformat(), row.entity.id,
                        )
                sql: dict[str, bool] = {}
                for kind, adapter in _adapters(pg).items():
                    mine = [row for row in rows if row.kind == kind]
                    table = f'"{pg.schema}"."{_table_name_for(_MODELS[kind])}"'
                    assert table.endswith(f'."{adapter.entity_table}"'), "the adapter's entity table is the model's table"
                    selected = await conn.fetch(
                        f"SELECT e.id FROM {table} e WHERE e.id = ANY($1::text[]) AND ({adapter.eligibility_sql()})",
                        [row.entity.id for row in mine],
                    )
                    chosen = {record["id"] for record in selected}
                    sql.update({row.label: row.entity.id in chosen for row in mine})
            finally:
                await transaction.rollback()
    finally:
        await pg.aclose()

    python = await _python_answers(sqlite_provider, rows, now)

    assert sql == python, "eligibility_sql and is_eligible disagree"
    assert python == {row.label: row.eligible for row in rows}
