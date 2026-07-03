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
