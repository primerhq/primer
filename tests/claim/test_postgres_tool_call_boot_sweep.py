"""The tool-call boot sweep against a real Postgres (BPF wave 1, the executor's step 9).

The sweep is new storage-touching code: a `where_in` predicate over the
task table's JSONB state, a per-row session read, and a direct fenced
`patch_if` void. The in-memory tests pin the decisions; only Postgres can
show the statements execute as written (the `where_in` renders, the fence
refuses a moved row, the lease delete lands in the same table the claim
engine reads).

Runs in the CI Postgres lane only (tests/pg_gate.py); no test here assumes a
default port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

import asyncpg

from primer.api._app_lifespan_phases import sweep_orphaned_tool_call_tasks
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import CLAIM_PRIORITY_RESUME, ClaimKind
from primer.model.agent import Agent, AgentModel
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.yields import tool_wait_event_key
from primer.storage.postgres import PostgresStorageProvider
from primer.worker.yield_runtime import ToolWaitParkedState
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url

_needs_pg = needs_postgres("tool-call boot sweep tests")

_SESSION_ID = "sweep-pg"


def _config(url: str) -> PostgresConfig:
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def pg_storage() -> AsyncIterator[PostgresStorageProvider]:
    sp = PostgresStorageProvider(_config(require_postgres_url("tool-call boot sweep tests")))
    await sp.initialize()
    # The model tables are lazy (created on first storage use), so on a fresh
    # database the cleanup deletes hit tables that do not exist yet. The
    # WorkspaceSession table is ``sessions`` (the model's storage name, not its
    # class name); the claim engine's lease table is ``sp.leases_table``.

    async def _cleanup(conn) -> None:
        for statement, param in (
            (f'DELETE FROM "{sp.schema}"."toolcalltask" WHERE id LIKE $1', f"{_SESSION_ID}%"),
            (f'DELETE FROM "{sp.schema}"."sessions" WHERE id LIKE $1', f"{_SESSION_ID}%"),
            (f'DELETE FROM {sp.leases_table} WHERE entity_id LIKE $1', f"{_SESSION_ID}%"),
        ):
            try:
                await conn.execute(statement, param)
            except asyncpg.UndefinedTableError:
                pass

    async with sp.pool.acquire() as conn:
        await _cleanup(conn)
    try:
        yield sp
    finally:
        async with sp.pool.acquire() as conn:
            await _cleanup(conn)
        await sp.aclose()


def _session_row(status: SessionStatus, parked_state: dict | None) -> WorkspaceSession:
    return WorkspaceSession(
        id=_SESSION_ID,
        workspace_id="ws-pg",
        binding=AgentSessionBinding(
            agent_id="agent-pg",
            agent_snapshot=Agent(
                model=AgentModel(profile_id="profile-pg"),
                description="boot-sweep pg agent",
                tools=[],
            ),
        ),
        binding_epoch=1,
        status=status,
        created_at=_now(),
        turn_no=0,
        parked_status="parked" if parked_state is not None else None,
        parked_state=parked_state,
        parked_event_key=(
            tool_wait_event_key(_SESSION_ID, scoped_task_id=list(parked_state["outstanding_task_ids"])[0])
            if parked_state is not None else None
        ),
        parked_at=_now() if parked_state is not None else None,
    )


def _park_blob(task_ids: list[str]) -> dict:
    return ToolWaitParkedState(
        outstanding_task_ids=task_ids,
        notifying_task_ids=[],
        event_key=tool_wait_event_key(_SESSION_ID, scoped_task_id=task_ids[0]),
        llm_messages=[],
        turn_no=0,
        started_at=_now(),
    ).to_jsonable()


@_needs_pg
@pytest.mark.asyncio
async def test_the_sweep_voids_the_orphan_and_keeps_the_live_row(pg_storage):
    """One sweep pass over the real backend: an ENDED session's queued row is
    written terminal FAILED (fenced patch_if) and its TOOL_CALL lease deleted;
    a row the session's live park references is left exactly as-is, lease
    included. The statements are the ones the lifespan runs before the pool
    starts."""
    task_storage = pg_storage.get_storage(ToolCallTask)
    session_storage = pg_storage.get_storage(WorkspaceSession)

    orphan_id = f"{_SESSION_ID}/x:tool:0:1"
    live_id = f"{_SESSION_ID}/x:tool:0:2"
    # Two rows of one session cannot both be live: the ENDED check wins over
    # the park reference, so the orphan lives on its OWN (ENDED) session row.
    ended_session_id = f"{_SESSION_ID}-ended"
    orphan_id = f"{ended_session_id}/x:tool:0:1"

    # The orphan's session is ENDED with no park.
    ended_session = _session_row(SessionStatus.ENDED, None)
    ended_session.id = ended_session_id
    await session_storage.create(ended_session)
    # The live row's session is RUNNING with a park referencing it.
    live_session = _session_row(
        SessionStatus.RUNNING, _park_blob([live_id]),
    )
    await session_storage.create(live_session)

    await task_storage.create(ToolCallTask(
        id=orphan_id,
        session_id=ended_session_id,
        turn_no=0,
        tool_name="workspace__write",
        state=ToolCallTaskState.QUEUED,
        record_seq=1,
        created_at=_now(),
    ))
    await task_storage.create(ToolCallTask(
        id=live_id,
        session_id=_SESSION_ID,
        turn_no=0,
        tool_name="workspace__write",
        state=ToolCallTaskState.QUEUED,
        record_seq=1,
        created_at=_now(),
    ))

    engine = PostgresClaimEngine(
        storage_provider=pg_storage,
        adapters={ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=task_storage)},
    )
    for task_id in (orphan_id, live_id):
        await engine.upsert(ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME)
    assert await engine.has_lease(ClaimKind.TOOL_CALL, orphan_id)
    assert await engine.has_lease(ClaimKind.TOOL_CALL, live_id)

    await sweep_orphaned_tool_call_tasks(engine, pg_storage)

    orphan = await task_storage.get(orphan_id)
    assert orphan is not None and orphan.state is ToolCallTaskState.FAILED
    assert orphan.last_error, "the void records why the task never ran"
    assert orphan.finished_at is not None
    assert not await engine.has_lease(ClaimKind.TOOL_CALL, orphan_id), (
        "the orphan's lease must be deleted"
    )
    live = await task_storage.get(live_id)
    assert live is not None and live.state is ToolCallTaskState.QUEUED, (
        "the live park's row is left exactly as-is"
    )
    assert await engine.has_lease(ClaimKind.TOOL_CALL, live_id), (
        "the live row keeps its lease for the pool to claim"
    )
