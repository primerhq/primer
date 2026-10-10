"""Two wakes of one multi-event park keep both leaves on a live Postgres, with a real row lock (ticket 01a122cc-effa, item 1; C6 of the #707 review).

Wake A runs the real flip (``durably_mark_session_resumable``) inside an open transaction, so it holds the row lock; wake B, which read the SAME snapshot,
runs the real flip on another connection and blocks on that lock; then A commits. Under READ COMMITTED Postgres re-checks B's guard against the version A
committed (EvalPlanQual) and, when it still holds, evaluates B's SET against that version too. The flip used to SET the whole ``parked_state`` it built from
its snapshot, so B replaced A's leaf; it now sets only its own leaves (``jsonb_set`` over the row's own ``data``), so both survive.

Two shapes, both from probe C6 (/var/tmp/review707/r3/probe_707_r3_pg_c6.py):

* The park is already ``resumable`` (a reply to a third gate landed before these two): A writes no guarded field to a new value, so B's guard holds on
  A's version and B is applied after A. Before this change B's write dropped A's leaf; with it, all three leaves stay. PR #723 does not change this case.
* The park is ``parked``: A moves ``parked_status`` from ``parked`` to ``resumable``, both of which B's guard allows. On main B is REFUSED instead of
  overwriting: ``compile_postgres`` renders a multi-valued guard as ``IN (SELECT jsonb_array_elements(..))``, whose EvalPlanQual re-check compares A's version
  with the one list element the first pass matched (``parked``); ``patch_if_checked`` counts that refusal as drift (``storage_cas_drift_total``) and B's
  reply is lost unless a later delivery of it flips the park again. PR #723 renders the guard as ``= ANY(ARRAY(..))``; with #723 alone B lands and drops A's
  leaf, so both leaves and no drift need both changes. This case is therefore a strict xfail while the guard still renders the ``IN`` form (#723 not
  merged); once it does not, the marker no longer applies and the test runs as a plain test.

Gated on the single Postgres test gate (``tests/pg_gate.py``); listed in ``LANE_FILES``, so the CI Postgres lane runs it in its own process. The
deterministic SQLite twin is ``test_park_flip_writes_leaves.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import pytest
import pytest_asyncio

import primer.observability.metrics as metrics
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.workspace_session import WorkspaceSession
from primer.session.yields import durably_mark_session_resumable
from primer.storage import postgres as pg_backend
from primer.storage._patch import compile_postgres, validate_patch
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url
from tests.session.test_machine_wake_fences import _graph_park

_WHAT = "the live park-flip leaf tests"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]

#: Every wait is bounded: a wake that never unblocks fails the test instead of hanging the lane.
_BOUND_S = 30.0


def _guard_renders_the_in_sub_select() -> bool:
    """Whether a multi-valued ``where`` still compiles to ``IN (SELECT ...)``, whose EvalPlanQual re-check refuses a row moved to another allowed value (#723)."""
    _, where_sql, _ = compile_postgres(*validate_patch({"x": 1}, {}, {"status": ["a", "b"]}), first_param=2)
    return "IN (SELECT" in where_sql


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


@pytest_asyncio.fixture
async def provider() -> AsyncIterator[PostgresStorageProvider]:
    """A provider on a throwaway schema of the gated database, dropped afterwards."""
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"pfl{uuid.uuid4().hex[:16]}"
    sp = PostgresStorageProvider(PostgresConfig(
        hostname=url.hostname or "localhost",
        port=explicit_port(url),
        username=url.username or "primer",
        password=url.password or "",  # type: ignore[arg-type]
        database=(url.path or "/").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    ))
    async with asyncio.timeout(_BOUND_S):
        await sp.initialize()
    try:
        yield sp
    finally:
        async with asyncio.timeout(_BOUND_S):
            async with sp.pool.acquire() as c:
                await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            # The "table ensured" cache is keyed by id(provider): a later provider at this address must not skip its CREATE.
            for key in [k for k in pg_backend._table_ensured if k[0] == id(sp)]:
                pg_backend._table_ensured.discard(key)
            await sp.aclose()


def _drift() -> float:
    return metrics.storage_cas_drift_total.labels("WorkspaceSession")._value.get()


def _gate(node: str, tcid: str) -> dict:
    return {"node_id": node, "tool_call_id": tcid, "event_key": f"ask_user:gs:{node}:{tcid}", "tool_name": "ask_user", "resume_metadata": {"prompt": node}}


def _leaves(row: WorkspaceSession) -> dict:
    return {entry["event_key"]: entry["payload"] for entry in ((row.parked_state or {}).get("resume_event_payloads") or {}).values()}


class _OnConnection:
    """The session storage seen through one connection: the flip's write (and the drift tripwire's re-read) run on ``conn``, inside its open transaction."""

    def __init__(self, storage: Any, conn: Any) -> None:
        self._storage, self._conn = storage, conn

    async def patch_if(self, id: str, patch: Any = None, *, where: Any, set_paths: Any = None, conn: Any = None) -> Any:  # noqa: A002
        return await self._storage.patch_if(id, patch, where=where, set_paths=set_paths, conn=self._conn)

    async def get(self, id: str, *, conn: Any = None) -> Any:  # noqa: A002
        return await self._storage.get(id, conn=self._conn)


async def _until_blocked_by(provider: PostgresStorageProvider, blocker: Any, task: asyncio.Task[Any]) -> None:
    """Return once a backend waits on a lock ``blocker``'s backend holds. Raises RuntimeError (a harness failure, never an expected failure) if ``task``
    ends first: B then never waited, and the race this file is about never ran. The caller bounds the wait."""
    blocker_pid = blocker.get_server_pid()
    async with provider.pool.acquire() as probe:
        while not task.done():
            if await probe.fetchval("SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))", blocker_pid):
                return
            await asyncio.sleep(0.02)
    raise RuntimeError("wake B finished without waiting on wake A's row lock, so the race was never run")


async def _race(
    provider: PostgresStorageProvider, a: tuple[WorkspaceSession, str], b: tuple[WorkspaceSession, str], *, payloads: tuple[dict, dict] | None = None,
) -> tuple[bool, bool]:
    """Wake A (``(snapshot, key)``) flips inside an open transaction and holds the row lock; wake B blocks on it; A commits. Returns what each flip
    returned. Each wake's payload is ``{"response": <its key>}`` unless ``payloads`` gives them. On any failure A is rolled back (which frees B) and B is
    awaited; every await is bounded."""
    payload_a, payload_b = payloads or ({"response": a[1]}, {"response": b[1]})
    sessions = provider.get_storage(WorkspaceSession)
    async with asyncio.timeout(3 * _BOUND_S), provider.pool.acquire() as c1:
        tx = c1.transaction()
        await tx.start()
        committed = False
        b_task: asyncio.Task[bool] | None = None
        try:
            async with asyncio.timeout(_BOUND_S):
                landed_a = await durably_mark_session_resumable(
                    a[0], event_key=a[1], payload=payload_a, session_storage=_OnConnection(sessions, c1), engine=None,
                )
                b_task = asyncio.create_task(durably_mark_session_resumable(
                    b[0], event_key=b[1], payload=payload_b, session_storage=sessions, engine=None,
                ))
                await _until_blocked_by(provider, c1, b_task)
                await tx.commit()
                committed = True
                return landed_a, await b_task
        finally:
            async with asyncio.timeout(_BOUND_S):
                if not committed:
                    await tx.rollback()
                if b_task is not None:
                    await asyncio.gather(b_task, return_exceptions=True)


async def test_two_wakes_of_a_resumable_multi_event_park_that_read_one_snapshot_keep_both_leaves(provider) -> None:
    """The park is already ``resumable``: a reply to n3 landed, then two replies (n1, n2) read the park and race on its row lock. Both land and all
    three replies stay, with no drift. Before this change B's write replaced the whole ``parked_state`` and dropped A's reply."""
    sessions = provider.get_storage(WorkspaceSession)
    n1, n2, n3 = _gate("n1", "call_1"), _gate("n2", "call_2"), _gate("n3", "call_3")
    async with asyncio.timeout(_BOUND_S):
        await sessions.create(_graph_park("gs", parked_at=datetime.now(UTC) - timedelta(minutes=1), agent_yields=(n1, n2, n3)))
        assert await durably_mark_session_resumable(
            await sessions.get("gs"), event_key=n3["event_key"], payload={"response": n3["event_key"]}, session_storage=sessions, engine=None,
        ) is True
        a, b = await sessions.get("gs"), await sessions.get("gs")
        assert a.parked_status == "resumable", "precondition: the third reply already flipped the park"

    landed = await _race(provider, (a, n1["event_key"]), (b, n2["event_key"]))

    async with asyncio.timeout(_BOUND_S):
        after = await sessions.get("gs")
    keys = [n1["event_key"], n2["event_key"], n3["event_key"]]
    assert {"landed": landed, "parked_status": after.parked_status, "leaves": _leaves(after), "drift": _drift()} == {
        "landed": (True, True), "parked_status": "resumable", "leaves": {key: {"response": key} for key in keys}, "drift": 0,
    }, "wake B, applied after wake A on its committed version, dropped A's reply"


@pytest.mark.xfail(
    _guard_renders_the_in_sub_select(), strict=True, raises=AssertionError,
    reason="#723 not merged: the multi-valued guard's EvalPlanQual re-check refuses wake B once A moved parked_status to resumable (drift 1)",
)
async def test_two_wakes_of_a_parked_multi_event_park_that_read_one_snapshot_keep_both_leaves(provider) -> None:
    """Probe C6 exactly: two replies read a freshly ``parked`` park and race on its row lock. Both must land, both replies must stay, and nothing may
    count as drift. On main B is refused by the guard's re-check (see the module docstring): ``landed`` is ``(True, False)``, one leaf, drift 1."""
    sessions = provider.get_storage(WorkspaceSession)
    n1, n2 = _gate("n1", "call_1"), _gate("n2", "call_2")
    async with asyncio.timeout(_BOUND_S):
        await sessions.create(_graph_park("gs", parked_at=datetime.now(UTC) - timedelta(minutes=1), agent_yields=(n1, n2)))
        a, b = await sessions.get("gs"), await sessions.get("gs")

    landed = await _race(provider, (a, n1["event_key"]), (b, n2["event_key"]))

    async with asyncio.timeout(_BOUND_S):
        after = await sessions.get("gs")
    keys = [n1["event_key"], n2["event_key"]]
    assert {"landed": landed, "parked_status": after.parked_status, "leaves": _leaves(after), "drift": _drift()} == {
        "landed": (True, True), "parked_status": "resumable", "leaves": {key: {"response": key} for key in keys}, "drift": 0,
    }


async def test_two_decisions_on_one_gate_that_read_one_snapshot_land_once_and_the_first_stands(provider) -> None:
    """Ticket 01a12606: two DIFFERENT decisions on the same gate read a freshly ``parked`` park and race on its row lock. The flip refuses a wake whose
    key already holds a leaf, in the statement's own guard, so Postgres re-checks it for B against the version A committed: B is refused, A's decision
    stands, and nothing counts as drift. Before, B's guard (the park, a status it may advance from) still held and B replaced A's decision."""
    sessions = provider.get_storage(WorkspaceSession)
    n1, n2 = _gate("n1", "call_1"), _gate("n2", "call_2")
    async with asyncio.timeout(_BOUND_S):
        await sessions.create(_graph_park("gs", parked_at=datetime.now(UTC) - timedelta(minutes=1), agent_yields=(n1, n2)))
        a, b = await sessions.get("gs"), await sessions.get("gs")

    landed = await _race(provider, (a, n1["event_key"]), (b, n1["event_key"]), payloads=({"response": "first"}, {"response": "second"}))

    async with asyncio.timeout(_BOUND_S):
        after = await sessions.get("gs")
    assert {"landed": landed, "parked_status": after.parked_status, "leaves": _leaves(after), "drift": _drift()} == {
        "landed": (True, False), "parked_status": "resumable", "leaves": {n1["event_key"]: {"response": "first"}}, "drift": 0,
    }, "the second decision on one gate replaced the first"
