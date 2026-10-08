"""The Playwright journey's seed must build where an event loop is already running.

``tests/ui_e2e/_delegation_seed.build()`` drives the real ``DelegationRecorder`` (async) to build its log, and used to do it with
``asyncio.run``. pytest-playwright runs the test body inside a running loop, so the journey's first CI run died with
``RuntimeError: asyncio.run() cannot be called from a running event loop`` before it asserted anything. The seed now runs the
recorder on a loop of its own thread whenever the caller's thread already has one.
"""

from __future__ import annotations

from tests.ui_e2e import _delegation_seed as seed


def test_the_seed_builds_with_no_loop_running() -> None:
    seeded = seed.build()
    assert len(seeded.records) == 12   # 11 plus the helper's own tool result, which the recorder writes (the seed used to leave it out)


async def test_the_seed_builds_inside_a_running_loop() -> None:
    """The same call a Playwright test makes: from synchronous code, in a thread whose loop is running."""
    seeded = seed.build()
    assert len(seeded.records) == 12   # 11 plus the helper's own tool result, which the recorder writes (the seed used to leave it out)
    assert [r["seq"] for r in seeded.records] == list(range(1, 13))
    assert any(r["payload"].get("delegate_run_id") == seed.RUN_GRANDCHILD for r in seeded.records)


async def test_two_builds_inside_a_running_loop_agree() -> None:
    assert seed.build().records == seed.build().records
