"""PostgresClaimEngine.watch_ready when its LISTEN connection DIES.

asyncpg never raises into the NOTIFY queue when the backend goes away, so a
watcher without a termination listener parks forever and the pool's
``_engine_bus_loop`` stops waking the claim loop (claims then only happen on the
poll interval). A drop must surface as a ``ListenConnectionLost`` so that loop's
restart arm re-subscribes.

The cleanup in ``watch_ready`` runs against a connection asyncpg has already
released, where every method raises ``InterfaceError``. That must never replace
the ``CancelledError`` a cancelled watcher owes its caller.

No database needed: ``tests._listen_fakes`` models asyncpg's drop contract.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimKind
from primer.model.except_ import ListenConnectionLost
from tests._listen_fakes import FakeListenPool, eventually, stop


def _engine() -> tuple[FakeListenPool, PostgresClaimEngine]:
    pool = FakeListenPool()
    engine = PostgresClaimEngine(
        storage_provider=SimpleNamespace(
            pool=pool, leases_table='"public"."leases"', schema="public",
        ),
        adapters={},
    )
    return pool, engine


async def _watch(engine: PostgresClaimEngine, got: list) -> None:
    async for item in engine.watch_ready():
        got.append(item)


async def test_a_terminated_listen_connection_surfaces_as_listen_connection_lost():
    pool, engine = _engine()
    got: list = []
    task = asyncio.create_task(_watch(engine, got))
    await eventually(lambda: pool.conns and pool.conns[0].listeners, what="LISTEN")
    pool.conns[0].notify("claim_ready", "session:s-1")
    await eventually(lambda: got == [(ClaimKind.SESSION, "s-1")], what="the pre-drop wake")

    pool.conns[0].drop()

    with pytest.raises(ListenConnectionLost, match="claim_ready"):
        await asyncio.wait_for(task, 3.0)
    assert pool.conns[0] in pool.released


async def test_wakes_received_before_the_drop_are_delivered_before_the_error():
    pool, engine = _engine()
    got: list = []
    task = asyncio.create_task(_watch(engine, got))
    await eventually(lambda: pool.conns and pool.conns[0].listeners, what="LISTEN")
    conn = pool.conns[0]
    conn.notify("claim_ready", "session:a")
    conn.notify("claim_ready", "harness:b")
    conn.drop()

    with pytest.raises(ListenConnectionLost):
        await asyncio.wait_for(task, 3.0)
    assert got == [(ClaimKind.SESSION, "a"), (ClaimKind.HARNESS, "b")]


async def test_cancelling_a_watcher_after_a_drop_raises_cancelled_error():
    """The bug this pins: cleanup called conn.remove_listener on the released
    proxy, so InterfaceError replaced CancelledError at shutdown."""
    pool, engine = _engine()
    got: list = []
    task = asyncio.create_task(_watch(engine, got))
    await eventually(lambda: pool.conns and pool.conns[0].listeners, what="LISTEN")

    pool.conns[0].drop()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3.0)
    assert pool.conns[0] in pool.released


@pytest.mark.parametrize(
    "failing, still_runs",
    [
        ("remove_termination_listener", "remove_listener"),
        ("remove_listener", "remove_termination_listener"),
    ],
)
async def test_each_cleanup_step_runs_even_if_the_other_fails(failing, still_runs, caplog):
    """One failing step must not skip the other, and the connection must still
    go back to the pool. Each failure is logged at DEBUG with its cause."""
    pool, engine = _engine()
    gen = engine.watch_ready()
    task = asyncio.create_task(gen.__anext__())
    await eventually(lambda: pool.conns and pool.conns[0].listeners, what="LISTEN")
    conn = pool.conns[0]
    conn.fail_methods.add(failing)
    conn.notify("claim_ready", "session:s-1")
    assert await asyncio.wait_for(task, 2.0) == (ClaimKind.SESSION, "s-1")

    with caplog.at_level(logging.DEBUG, logger="primer.claim.postgres"):
        await gen.aclose()

    assert still_runs in conn.calls
    assert conn in pool.released
    assert f"boom: {failing}" in caplog.text


async def test_a_watcher_that_is_never_dropped_still_cleans_up_normally():
    """Control: the happy path removes both callbacks and releases once."""
    pool, engine = _engine()
    got: list = []
    task = asyncio.create_task(_watch(engine, got))
    await eventually(lambda: pool.conns and pool.conns[0].listeners, what="LISTEN")
    conn = pool.conns[0]
    assert len(conn.termination_listeners) == 1

    await stop(task)

    assert conn.termination_listeners == set()
    assert conn.listeners == {}
    assert pool.released == [conn]
