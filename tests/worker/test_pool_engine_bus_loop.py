"""WorkerPool._engine_bus_loop: how it reacts when the claim watcher fails.

The loop is only a wake hint for the claim loop (which also polls), but it is
the one place that notices the watcher died, so it is where a dropped LISTEN
connection must become (a) a re-subscribe and (b) an operator-visible line.

* A lost LISTEN connection is EXPECTED (a Postgres restart, a failover): one
  WARNING line naming the channel and the cause, no ERROR, no traceback.
* Anything else the watcher raises is unexpected: ERROR with the traceback.
* The restart wait doubles while the watcher keeps failing, but a watcher that
  stayed up is healthy again, so the NEXT drop starts from the short wait.
  Before this was reachable by a real drop the wait only ever reset on a clean
  generator exit, which a watcher never makes, so N spaced drops ratcheted
  every later restart to the 30s cap.

No database needed. ``primer.worker.pool``'s own ``asyncio.sleep`` and
``time.monotonic`` are swapped for recorders (the module attributes only, never
the global event loop clock).
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import asyncpg
import pytest

import primer.worker.pool as pool_module
from primer.claim.postgres import PostgresClaimEngine
from primer.model.except_ import ListenConnectionLost
from primer.model.scheduler import WorkerConfig
from primer.worker.pool import WorkerPool
from tests._listen_fakes import FakeListenPool, eventually

# The restart wait: 1s, doubling, capped at 30s. A watcher that stayed up at
# least as long as the cap is considered healthy.
INITIAL = 1.0
CAP = 30.0

LOST = ListenConnectionLost("claim_ready LISTEN connection was terminated")


class _Patched:
    """A module stand-in that forwards everything but the named overrides."""

    def __init__(self, real, **overrides):
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._real, name)


@pytest.fixture
def fake_time(monkeypatch):
    """Recorded, instant restart sleeps plus a clock the watchers advance."""
    real_sleep = asyncio.sleep
    state = SimpleNamespace(now=0.0, delays=[])

    async def fake_sleep(delay, *args, **kwargs):
        state.delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(pool_module, "asyncio", _Patched(asyncio, sleep=fake_sleep))
    monkeypatch.setattr(pool_module, "time", _Patched(time, monotonic=lambda: state.now))
    return state


def _pool(engine) -> WorkerPool:
    return WorkerPool(
        config=WorkerConfig(concurrency=1, poll_interval_seconds=0.1),
        scheduler=None,                                # type: ignore[arg-type]
        storage=None,                                  # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
        engine=engine,
    )


class _ScriptedEngine:
    """Each watch_ready() call runs for the next scripted time, then raises the
    scripted exception. Once the script is spent it asks the loop to stop."""

    def __init__(self, script, clock=None):
        self.script = list(script)
        self.clock = clock
        self.pool: WorkerPool | None = None

    async def watch_ready(self):
        if not self.script:
            assert self.pool is not None
            self.pool._stopping.set()
            return
        ran_for, exc = self.script.pop(0)
        if self.clock is not None:
            self.clock.now += ran_for
        raise exc
        yield  # pragma: no cover  (makes this an async generator)


async def _run(script, fake_time) -> list[float]:
    engine = _ScriptedEngine(script, clock=fake_time)
    pool = _pool(engine)
    engine.pool = pool
    await asyncio.wait_for(pool._engine_bus_loop(), timeout=5.0)
    return fake_time.delays


async def test_a_lost_listen_connection_is_a_warning_without_a_traceback(fake_time, caplog):
    with caplog.at_level(logging.DEBUG, logger="primer.worker.pool"):
        await _run([(0.1, LOST)], fake_time)

    records = [r for r in caplog.records if r.name == "primer.worker.pool"]
    assert [r.levelname for r in records if r.levelno >= logging.WARNING] == ["WARNING"]
    (warning,) = [r for r in records if r.levelno == logging.WARNING]
    assert "lost its connection" in warning.getMessage()
    assert "claim_ready" in warning.getMessage()
    assert "terminated" in warning.getMessage()
    assert warning.exc_info is None


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 5432)"),
        ConnectionResetError("connection reset by peer"),
        TimeoutError("timed out waiting for a pooled connection"),
        # asyncpg's own errors while Postgres restarts. None is an OSError.
        asyncpg.CannotConnectNowError("the database system is starting up"),
        asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
        asyncpg.AdminShutdownError("terminating connection due to administrator command"),
        asyncpg.CrashShutdownError("terminating connection because of crash of another server process"),
    ],
    ids=["refused", "reset", "timeout", "starting-up", "connection-does-not-exist",
         "admin-shutdown", "crash-shutdown"],
)
async def test_a_failed_resubscribe_is_a_warning_that_does_not_claim_a_loss(
    failure, fake_time, caplog,
):
    """While the server is down or restarting, the re-subscribe fails with a
    builtin ConnectionError or timeout, or with one of asyncpg's own errors
    (neither of which is an OSError): "starting up" (57P03), a connection that
    does not exist mid-LISTEN (08xxx), or a shutdown interrupting it (57P01 and
    57P02). The watcher never reached a subscribed state, so this is not a
    lost LISTEN connection: the line must say it could not re-subscribe, give
    the cause, and carry no traceback."""
    with caplog.at_level(logging.DEBUG, logger="primer.worker.pool"):
        await _run([(0.1, failure)], fake_time)

    records = [r for r in caplog.records if r.name == "primer.worker.pool"]
    assert [r.levelname for r in records if r.levelno >= logging.WARNING] == ["WARNING"]
    (warning,) = [r for r in records if r.levelno == logging.WARNING]
    message = warning.getMessage()
    assert "could not re-subscribe" in message
    assert str(failure) in message
    assert "lost its connection" not in message
    assert warning.exc_info is None


async def test_a_failed_resubscribe_still_backs_off(fake_time):
    delays = await _run([(0.1, ConnectionRefusedError("down"))] * 4, fake_time)

    assert delays == [INITIAL, 2.0, 4.0, 8.0]


def test_a_lost_connection_is_both_a_primer_error_and_a_connection_error():
    from primer.model.except_ import PrimerError

    err = ListenConnectionLost("claim_ready LISTEN connection was terminated")

    assert isinstance(err, PrimerError)
    assert isinstance(err, ConnectionError)
    assert str(err) == "claim_ready LISTEN connection was terminated"


async def test_an_unexpected_watcher_failure_is_still_an_error_with_a_traceback(fake_time, caplog):
    with caplog.at_level(logging.DEBUG, logger="primer.worker.pool"):
        await _run([(0.1, ValueError("malformed claim_ready payload"))], fake_time)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[0] is ValueError


async def test_the_restart_wait_doubles_to_the_cap_while_the_watcher_keeps_failing(fake_time):
    delays = await _run([(0.1, LOST)] * 7, fake_time)

    assert delays == [INITIAL, 2.0, 4.0, 8.0, 16.0, CAP, CAP]


async def test_the_restart_wait_resets_after_a_watcher_that_stayed_up(fake_time):
    """Two drops spaced well apart must not ratchet."""
    script = [
        (0.1, LOST),          # crash-looping: 1s
        (0.1, LOST),          # still crash-looping: 2s
        (CAP + 60.0, LOST),   # ran a long time, then dropped: a fresh incident
        (0.1, LOST),          # and the next one backs off from the start again
    ]

    delays = await _run(script, fake_time)

    assert delays == [INITIAL, 2.0, INITIAL, 2.0]


async def test_the_reset_threshold_is_the_cap(fake_time):
    delays = await _run([(0.1, LOST), (CAP - 0.5, LOST), (CAP, LOST)], fake_time)

    assert delays == [INITIAL, 2.0, INITIAL]


async def test_the_claim_loop_is_woken_again_after_a_drop(fake_time):  # fake_time: instant restart
    """End to end over the real PostgresClaimEngine (fake pool): a drop is
    detected, the loop re-subscribes, and a later claim_ready wakes the claim
    loop. Before the fix the watcher parked forever on the dead connection."""
    fake_pool = FakeListenPool()
    engine = PostgresClaimEngine(
        storage_provider=SimpleNamespace(
            pool=fake_pool, leases_table='"public"."leases"', schema="public",
        ),
        adapters={},
    )
    pool = _pool(engine)
    task = asyncio.create_task(pool._engine_bus_loop())
    try:
        await eventually(lambda: fake_pool.conns and fake_pool.conns[0].listeners, what="LISTEN")
        fake_pool.conns[0].notify("claim_ready", "session:before")
        await eventually(pool._wake.is_set, what="the pre-drop wake")
        pool._wake.clear()

        fake_pool.conns[0].drop()

        await eventually(
            lambda: len(fake_pool.conns) == 2 and fake_pool.conns[1].listeners,
            what="a re-subscribe after the drop",
        )
        fake_pool.conns[1].notify("claim_ready", "session:after")
        await eventually(pool._wake.is_set, what="the post-drop wake")
    finally:
        pool._stopping.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_shutdown_after_a_drop_logs_no_error(fake_time, caplog):
    """drain_and_stop sets _stopping and then cancels the loop. If the watcher's
    cleanup runs against the already-released connection and raises
    InterfaceError, that replaces the CancelledError, and the loop logs an ERROR
    traceback for an expected shutdown."""
    fake_pool = FakeListenPool()
    engine = PostgresClaimEngine(
        storage_provider=SimpleNamespace(
            pool=fake_pool, leases_table='"public"."leases"', schema="public",
        ),
        adapters={},
    )
    pool = _pool(engine)
    task = asyncio.create_task(pool._engine_bus_loop())
    await eventually(lambda: fake_pool.conns and fake_pool.conns[0].listeners, what="LISTEN")

    fake_pool.conns[0].drop()
    pool._stopping.set()
    task.cancel()

    with caplog.at_level(logging.DEBUG, logger="primer.worker.pool"):
        await asyncio.wait_for(task, timeout=3.0)   # returns; does not raise

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
