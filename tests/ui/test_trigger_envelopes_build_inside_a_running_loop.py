"""The REAL trigger envelopes and fire bodies build inside a running event loop too (round 3 of #927).

``tests/_support/trigger_envelopes.py`` drives the real route over the real service, and round 2 seeded its storage with ``asyncio.run``. The
Playwright journeys (``tests/ui_e2e/test_trigger_list_row_actions_journey.py``, ``tests/ui_e2e/test_trigger_refusals_journey.py``) call
``real_envelopes()`` from the sync Playwright API, whose thread already runs an event loop, so ``asyncio.run`` refused (``RuntimeError: asyncio.run()
cannot be called from a running event loop``) and both journeys failed in CI. These call both builders from inside a running loop, as those journeys
do, without running a browser.
"""

from __future__ import annotations

import asyncio

from tests._support.trigger_envelopes import real_envelopes, real_fire_bodies


async def test_real_envelopes_build_inside_a_running_loop() -> None:
    asyncio.get_running_loop()  # the premise: this runs inside a loop, as the sync Playwright API's thread does

    envelopes = real_envelopes()

    assert envelopes["fire_refused"]["status"] == 403 and envelopes["fire_refused"]["extensions"]["code"] == "access_denied"
    assert envelopes["session_ended"]["extensions"]["error"] == "auth_required"


async def test_real_fire_bodies_build_inside_a_running_loop() -> None:
    asyncio.get_running_loop()

    bodies = real_fire_bodies()

    assert bodies["skipped"] == {"skipped": True, "fire_id": None, "results": []}
    assert bodies["fired"]["fire_id"] == "fire-1" and bodies["fired"]["results"][0]["artefact_id"] == "sess-1"


def test_both_still_build_with_no_loop_running() -> None:
    assert real_envelopes()["fire_refused"]["extensions"]["code"] == "access_denied"
    assert set(real_fire_bodies()) == {"skipped", "fired", "no_subscriptions", "partial", "all_failed", "delivery_skipped"}
