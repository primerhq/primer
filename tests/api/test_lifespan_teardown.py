"""Characterization test for the production lifespan teardown + backfill seams.

Guards the app.py lifespan decomposition (BE11). Building the app in
``API_PLUS_WORKER`` and running the full startup -> teardown cycle must:

* (a) never leak an exception out of the reverse-order ``finally`` block,
* (b) leave the core subsystems wired on ``app.state``, and
* (c) keep a post-startup bus/channel publish working — which only holds if
  the two construct-then-backfill seams stay intact
  (``channel_inbox._event_bus = event_bus`` and
  ``channel_registry.set_claim_engine(...)``).

Any accidental disturbance to the startup ordering, the teardown block, or
those seams during the phase extraction trips this test.

Mirrors how ``tests/api/test_runtime_modes.py`` builds the app: a fake
in-memory storage provider steered in via the
``primer.api.app._build_storage_provider`` patch seam.
"""

from __future__ import annotations

import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.model.scheduler import RuntimeMode

from tests.api.conftest import _FakeStorageProvider


@pytest.fixture
def mock_storage_provider() -> _FakeStorageProvider:
    return _FakeStorageProvider()


@pytest.mark.asyncio
async def test_lifespan_full_cycle_preserves_state_and_seams(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None)
    app = create_app(cfg)

    # (a) No exception escapes the reverse-order finally: if any teardown step
    # re-raised, exiting this async-with would propagate it and fail the test.
    async with app.router.lifespan_context(app):
        # (b) The core subsystems are wired on app.state.
        assert app.state.worker_pool is not None
        assert app.state.claim_engine is not None
        assert app.state.event_bus is not None
        assert app.state.channel_registry is not None
        assert app.state.scheduler is not None

        # The two construct-then-backfill seams are intact:
        #  - channel_inbox was built early with event_bus=None, then rebound to
        #    the bus built later in the lifespan.
        #  - channel_registry received the claim engine built later.
        assert (
            app.state.channel_inbox._event_bus is app.state.event_bus  # noqa: SLF001
        )
        assert (
            app.state.channel_registry._claim_engine  # noqa: SLF001
            is app.state.claim_engine
        )

        # (c) A post-startup bus/channel publish succeeds. Raises if the bus
        # were closed/None or the inbox backfill seam were broken.
        await app.state.event_bus.publish(
            "test:lifespan-probe", {"ok": True}
        )


@pytest.mark.asyncio
async def test_lifespan_mcp_mount_starts_and_stops_in_one_task_and_pins_nothing(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Production check for the leak the test ``app`` fixture had.

    The MCP mount runs an anyio task group, which must be exited by the task
    that entered it. The test fixture violated that (setup and teardown run
    in different tasks) and pinned every finished app through anyio's
    ``_task_states``. The production lifespan starts and stops the mount in
    one coroutine, so it should be immune - established by reading the code
    until this test. Uvicorn drives the whole lifespan from ONE task, so one
    dedicated task does the same here.

    The lifespan guards each teardown step (so one failure does not skip the
    rest), which means a cross-task exit does NOT raise out of it: it is
    logged as "mcp session manager teardown failed" at ERROR. So the first
    check below is that record's absence. A pinned entry, if one existed,
    would survive the garbage collection at the end, and the app would stay
    alive. Verified with a deliberate cross-task enter/exit of the same
    lifespan: it logs that error and leaves the entry pinned.
    """
    import asyncio
    import gc
    import logging
    import weakref

    from anyio._backends import _asyncio as anyio_asyncio

    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    app = create_app(
        AppConfig(runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None)
    )
    app_ref = weakref.ref(app)
    mount_was_live: list[bool] = []
    running = asyncio.Event()
    release = asyncio.Event()

    async def _drive_lifespan() -> None:
        async with app.router.lifespan_context(app):
            mount_was_live.append(app.state.mcp_session_manager is not None)
            running.set()
            await release.wait()

    gc.collect()
    states_before = len(anyio_asyncio._task_states)  # noqa: SLF001

    caplog.set_level(logging.ERROR, logger="primer.api._app_lifespan")
    task = asyncio.create_task(_drive_lifespan(), name="lifespan-under-test")
    await running.wait()
    release.set()
    await task

    teardown_failures = [
        r.getMessage() for r in caplog.records
        if "mcp session manager teardown failed" in r.getMessage()
    ]
    assert not teardown_failures, (
        "the MCP mount's teardown failed (a cross-task anyio exit logs "
        f"exactly this): {teardown_failures}"
    )

    # Without this the test would pass vacuously if the mount never started.
    assert mount_was_live == [True]
    assert app.state.mcp_session_manager is None, "teardown did not run"

    del task, app, _drive_lifespan
    # Yield to the loop before collecting. In THIS loop iteration the handle
    # that resumed us after `await task` is still executing and still holds
    # the finished lifespan task as its argument, so the task (and its
    # WeakKeyDictionary entry) cannot be collected yet. That is the event
    # loop's own transient reference, not a pin - checked: one entry in this
    # iteration, zero after two yields. Collecting without yielding fails
    # even though nothing leaked.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()
    gc.collect()
    assert len(anyio_asyncio._task_states) <= states_before, (  # noqa: SLF001
        "the lifespan left a task-state entry pinned in anyio"
    )
    assert app_ref() is None, "the app is still alive after its lifespan ended"
