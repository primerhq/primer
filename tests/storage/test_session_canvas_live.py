"""A click and a batch from two processes both land on a live Postgres (pivot P5, Task 8; plan Review Focus 5).

Two pods write one session's canvas: an API pod records a user's click while a worker applies the agent's batch. Every write is a
``patch_if`` fenced on the ``rev`` and ``event_seq`` it read, so whichever lands second is REJECTED by the database, reads the row the
first one wrote and decides again. The in-memory fake serialises its writers (nothing it does yields between a read and the write), so
only a real database shows that this holds: two ``PostgresStorageProvider``s on one throwaway schema stand for the two pods (two
pools), and a barrier makes both read the row before either writes, so the collision is certain, not likely.

The record rule (audit G8-11) runs here too: an API pod's operator batch, written outside a turn, reserves the next seq of the
session log on the ``WorkspaceSession`` row guarded on the turn NOT running, while a worker pod flips that row to running for a turn
(``primer.session.dispatch._flip_to_running``, guarded on the status only). The database orders the two fenced writes: a flip first
makes the reservation fail and the record is skipped; a reservation first leaves the flip to land on a row whose ``last_seq`` already
holds the record's seq, so the turn numbers its records after it. In that order the record is appended after the turn began: the
residual Task 24's canvas.md documents (a console tailing with ``after_seq`` meets it only when it refetches).

Gate: ``PRIMER_TEST_POSTGRES_URL`` (see ``tests/pg_gate.py``). The tests skip without it and fail in the CI Postgres lane, where
``PRIMER_REQUIRE_POSTGRES_TESTS=1``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import pytest_asyncio

import primer.observability.metrics as metrics
from primer.canvas.lang import parse_program
from primer.canvas.service import CanvasService
from primer.model.canvas import SessionCanvas
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import _flip_to_running
from primer.storage.postgres import PostgresStorageProvider
from tests.canvas._fakes import FakeRegistry
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_WHAT = "the canvas concurrency test"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]

#: Every wait is bounded: a writer that never unblocks fails the test instead of hanging the lane.
_BOUND_S = 30.0
_PLAYLIST = Path(__file__).resolve().parents[1] / "canvas" / "golden" / "playlist_create.cl"


@dataclass
class Pods:
    """Two services over two providers (two pools) on one schema, as two pods of a deployment."""

    api: CanvasService
    worker: CanvasService
    api_sp: PostgresStorageProvider
    worker_sp: PostgresStorageProvider


def _provider(url: Any, schema: str) -> PostgresStorageProvider:
    return PostgresStorageProvider(PostgresConfig(
        hostname=url.hostname or "localhost",
        port=explicit_port(url),
        username=url.username or "primer",
        password=url.password or "",  # type: ignore[arg-type]
        database=(url.path or "/").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    ))


@pytest_asyncio.fixture
async def pods() -> AsyncIterator[Pods]:
    """The two pods, on a throwaway schema of the gated database that is dropped afterwards."""
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"cnv{uuid.uuid4().hex[:16]}"
    api_sp, worker_sp = _provider(url, schema), _provider(url, schema)
    async with asyncio.timeout(_BOUND_S):
        await api_sp.initialize()
        await worker_sp.initialize()
        for sp in (api_sp, worker_sp):
            await sp.get_event_store().ensure_schema()      # so canvas.updated and canvas.event land in a real event log
    try:
        yield Pods(CanvasService(api_sp), CanvasService(worker_sp), api_sp, worker_sp)
    finally:
        async with asyncio.timeout(_BOUND_S):
            async with api_sp.pool.acquire() as conn:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            for sp in (api_sp, worker_sp):
                await sp.aclose()


async def _seed_playlist(pods: Pods) -> None:
    """The playlist at rev 1, written by the API pod; the worker pod has read the row once, so its handle and table are warm."""
    await pods.api.apply(
        session_id="s1", workspace_id="ws-1", statements=parse_program(_PLAYLIST.read_text(encoding="utf-8")), unit="line",
        base_rev=None, source="operator", by="alice",
    )
    assert (await pods.worker_sp.get_storage(SessionCanvas).get("s1")).rev == 1


class _Writes:
    """What the pods' fenced writes came to: one entry per ``patch_if``, ``True`` when the database applied it."""

    def __init__(self) -> None:
        self.landed: list[bool] = []


def _meet_before_the_first_write(sps: list[PostgresStorageProvider]) -> _Writes:
    """Make every pod's FIRST fenced write wait until all of them have reached it, so all have read the same row before any writes.

    Only the first: a writer that lost the race retries alone, and a barrier that waited for a partner would hold it for ever.
    Returns what the writes came to, so a test can see that the collision really happened.
    """
    barrier, writes = asyncio.Barrier(len(sps)), _Writes()
    for sp in sps:
        store = sp.get_storage(SessionCanvas)
        real, first = store.patch_if, [True]

        async def patch_if(id, patch=None, *, where, set_paths=None, conn=None, _real=real, _first=first):  # noqa: A002
            if _first[0]:
                _first[0] = False
                await barrier.wait()
            written = await _real(id, patch, where=where, set_paths=set_paths, conn=conn)
            writes.landed.append(written is not None)
            return written

        store.patch_if = patch_if
    return writes


def _drift() -> float:
    return metrics.storage_cas_drift_total.labels("SessionCanvas")._value.get()


async def _events(pods: Pods, event_type: str) -> list[Any]:
    return [e for e in await pods.api_sp.get_event_store().read_after(0, limit=1000) if e.event_type == event_type]


async def test_a_click_and_a_batch_from_two_processes_both_land(pods: Pods):          # Review Focus 5
    await _seed_playlist(pods)
    writes, drift = _meet_before_the_first_write([pods.api_sp, pods.worker_sp]), _drift()
    async with asyncio.timeout(_BOUND_S):
        applied, clicked = await asyncio.gather(
            pods.worker.apply(
                session_id="s1", workspace_id="ws-1", statements=parse_program('skip = Button("Skip")\ncontrols += [skip]'), unit="line",
                base_rev=None, source="agent", by="agent",
            ),
            pods.api.record_event(
                session_id="s1", workspace_id="ws-1", element="genre", kind="change", value="Lo-fi", by="alice",
            ),
        )
    assert sorted(writes.landed) == [False, True, True], "both read rev 1 and wrote; the database refused one, which retried and landed"
    assert _drift() == drift, "a lost race is not the CAS drift alarm"
    row = await pods.api_sp.get_storage(SessionCanvas).get("s1")
    assert "skip" in row.elements and row.elements["controls"]["children"] == ["genre", "play", "skip"]
    assert row.values["genre"] == "Lo-fi" and row.value_set_by["genre"] == "user"
    assert (row.rev, row.event_seq) == (3, 1)
    assert sorted([applied.rev, clicked.rev]) == [2, 3], "the two writes took successive revs, in whichever order they landed"
    assert [e.id for e in row.events] == ["e1"] and row.events[0].value == "Lo-fi"
    assert [e.payload["rev"] for e in await _events(pods, "canvas.updated")] == [1, applied.rev]
    assert [e.payload["id"] for e in await _events(pods, "canvas.event")] == ["e1"]


async def test_clicks_from_two_processes_each_get_their_own_event_seq(pods: Pods):
    await _seed_playlist(pods)
    async with asyncio.timeout(_BOUND_S):
        events = await asyncio.gather(*(
            pods.api.record_event(session_id="s1", workspace_id="ws-1", element="play", kind="click", by=f"api{n}")
            if n % 2 == 0 else
            pods.worker.record_event(session_id="s1", workspace_id="ws-1", element="play", kind="click", by=f"worker{n}")
            for n in range(4)
        ))
    row = await pods.api_sp.get_storage(SessionCanvas).get("s1")
    assert sorted(e.seq for e in events) == [1, 2, 3, 4] and row.event_seq == 4 and row.rev == 1
    assert sorted(e.id for e in row.events) == ["e1", "e2", "e3", "e4"] and len(row.wake_times) == 4
    assert len(await _events(pods, "canvas.event")) == 4


async def test_two_batches_from_two_processes_both_land(pods: Pods):
    await _seed_playlist(pods)
    writes = _meet_before_the_first_write([pods.api_sp, pods.worker_sp])
    async with asyncio.timeout(_BOUND_S):
        first, second = await asyncio.gather(
            pods.api.apply(
                session_id="s1", workspace_id="ws-1", statements=parse_program('one = Text("from the api")\nroot += [one]'), unit="line",
                base_rev=None, source="operator", by="alice",
            ),
            pods.worker.apply(
                session_id="s1", workspace_id="ws-1", statements=parse_program('two = Text("from the worker")\nroot += [two]'), unit="line",
                base_rev=None, source="agent", by="agent",
            ),
        )
    assert sorted(writes.landed) == [False, True, True]
    row = await pods.api_sp.get_storage(SessionCanvas).get("s1")
    assert {"one", "two"} <= set(row.elements) and sorted(row.elements["root"]["children"][-2:]) == ["one", "two"]
    assert row.rev == 3 and sorted([first.rev, second.rev]) == [2, 3]


# ---- the record rule with a workspace registry (audit G8-11) ---------------------------------------------------------------------------


async def _seed_idle_session(sp: PostgresStorageProvider, *, last_seq: int) -> WorkspaceSession:
    from datetime import UTC, datetime

    row = WorkspaceSession(
        id="s1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="agent-s1"), status=SessionStatus.WAITING,
        created_at=datetime.now(UTC), turn_status="idle", last_seq=last_seq,
    )
    return await sp.get_storage(WorkspaceSession).create(row)


def _order_the_session_writes(pods: Pods, first: str | None) -> dict[str, list[bool]]:
    """Make each pod's FIRST fenced write of the session row wait until both pods have read it, then let ``first`` ("api" or "worker")
    land before the other one is sent; ``None`` sends both at once and lets the database order them. Returns, per pod, whether each of
    its writes landed."""
    barrier, landed = asyncio.Barrier(2), {"api": [], "worker": []}
    done = {"api": asyncio.Event(), "worker": asyncio.Event()}
    for name, sp in (("api", pods.api_sp), ("worker", pods.worker_sp)):
        store = sp.get_storage(WorkspaceSession)
        real, untouched = store.patch_if, [True]

        async def patch_if(id, patch=None, *, where, set_paths=None, conn=None, _real=real, _untouched=untouched, _name=name):  # noqa: A002
            if _untouched[0]:
                _untouched[0] = False
                await barrier.wait()
                other = "worker" if _name == "api" else "api"
                if first == other:
                    await done[other].wait()
            try:
                written = await _real(id, patch, where=where, set_paths=set_paths, conn=conn)
            finally:
                done[_name].set()
            landed[_name].append(written is not None)
            return written

        store.patch_if = patch_if
    return landed


async def _race_a_record_against_a_running_flip(pods: Pods, first: str | None) -> tuple[Any, WorkspaceSession | None, dict, FakeRegistry]:
    """The API pod applies an operator batch (its record reserves a seq outside a turn) while the worker pod flips the session to running."""
    from datetime import UTC, datetime

    await _seed_playlist(pods)
    registry = FakeRegistry()
    api = CanvasService(pods.api_sp, workspace_registry=registry)
    worker_sessions = pods.worker_sp.get_storage(WorkspaceSession)
    session = await worker_sessions.get("s1")                       # the worker read the row when it claimed the session
    landed = _order_the_session_writes(pods, first)
    async with asyncio.timeout(_BOUND_S):
        applied, (flipped, refused) = await asyncio.gather(
            api.apply(
                session_id="s1", workspace_id="ws-1", statements=parse_program('note = Text("from the operator")\nroot += [note]'),
                unit="line", base_rev=None, source="operator", by="alice",
            ),
            _flip_to_running(worker_sessions, session, datetime.now(UTC)),
        )
    assert refused is None and flipped is not None, "the flip is guarded on the status only, so it always lands"
    return applied, flipped, landed, registry


async def test_a_flip_to_running_first_leaves_no_record(pods: Pods):
    await _seed_idle_session(pods.api_sp, last_seq=5)
    applied, flipped, landed, registry = await _race_a_record_against_a_running_flip(pods, first="worker")
    assert landed == {"api": [False], "worker": [True]}, "the database refused the reservation the running flip overtook"
    row = await pods.api_sp.get_storage(WorkspaceSession).get("s1")
    assert (row.turn_status, row.last_seq) == ("running", 5), "no seq was reserved under a running turn"
    assert registry.workspace("ws-1").lines("s1") == [], "the record is skipped; the batch is in the canvas and the event log"
    assert applied.rev == 2 and "note" in (await pods.api_sp.get_storage(SessionCanvas).get("s1")).elements


async def test_a_reservation_first_puts_the_record_before_the_turns_seqs(pods: Pods):
    await _seed_idle_session(pods.api_sp, last_seq=5)
    applied, flipped, landed, registry = await _race_a_record_against_a_running_flip(pods, first="api")
    assert landed == {"api": [True], "worker": [True]}
    [record] = registry.workspace("ws-1").lines("s1")
    assert (record["kind"], record["seq"], record["payload"]["rev"]) == ("canvas_op", 6, applied.rev)
    assert flipped.last_seq == 6 and flipped.turn_status == "running", "the turn seeds past the record's seq: no seq is used twice"


async def test_a_record_racing_a_running_flip_never_reuses_a_seq(pods: Pods):
    await _seed_idle_session(pods.api_sp, last_seq=5)
    _applied, flipped, landed, registry = await _race_a_record_against_a_running_flip(pods, first=None)
    lines = registry.workspace("ws-1").lines("s1")
    row = await pods.api_sp.get_storage(WorkspaceSession).get("s1")
    assert landed["worker"] == [True] and row.turn_status == "running"
    if lines:
        assert [r["seq"] for r in lines] == [6] and row.last_seq == 6 and flipped.last_seq == 6
    else:
        assert landed["api"] == [False] and row.last_seq == 5
