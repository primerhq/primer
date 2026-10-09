"""A timer fire wakes the sleep it was published for, not another one that shares its event key (security ticket 01a12151-b225; same mechanism as 01a1208d).

The ``sleep`` tool parked on ``timer:{tool_call_id}``: no session in the key. A provider repeats ``call_0``, ``call_1`` ... in every conversation, so two
sessions sleeping at once (or one session sleeping twice) waited on ONE key. The ``TimerScheduler`` publishes an empty payload on a key when a row parked on it is
due, and the flip advances EVERY row parked on that key without looking at its own deadline: session A's timer woke session B's sleep hours early, and a fire
redelivered after a session slept again under the same id woke the new sleep.

Two changes. The key carries the session (``timer:{session_id}:{tool_call_id}``, as ``ask_user`` and ``watch`` do), so one session's timer is not another's key. And a
timer fire, like a timeout marker, applies only to a park whose deadline has passed (``parked_until`` no more than a few seconds ahead of the flipping node's
clock); that covers a park written before this release on the shared key, and the redelivery within one session, which a session-scoped key cannot tell apart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import primer.observability.metrics as metrics
from primer.bus.scheduler_tasks import TimerScheduler
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.session.yields import durably_mark_session_resumable, flip_sessions_parked_on
from primer.toolset.misc import _sleep_handler
from primer.worker.yield_runtime import make_cancelled_payload
from tests.conftest import _FakeStorageProvider

SHARED = "timer:call_0"            # the key a park written before this release sits on


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


def _sleeping(session_id: str, *, until: timedelta | None, key: str = SHARED) -> WorkspaceSession:
    now = datetime.now(UTC)
    return WorkspaceSession(
        id=session_id, workspace_id="ws-t", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING, created_at=now,
        parked_status="parked", parked_at=now, parked_until=None if until is None else now + until, parked_event_key=key,
        parked_state={"tool_call_id": "call_0", "yielded": {"tool_name": "sleep", "event_key": key, "resume_metadata": {"requested_seconds": 60}}},
    )


async def _flip(row: WorkspaceSession, payload: dict, key: str = SHARED):
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(row)
    did = await durably_mark_session_resumable(row, event_key=key, payload=payload, session_storage=storage, engine=None)
    return did, await storage.get(row.id)


# ---- the key --------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_sessions_sleeping_under_the_same_provider_id_wait_on_different_keys() -> None:
    first = await _sleep_handler({"seconds": 60}, ctx=ToolContext(tool_call_id="call_0", session_id="s1", workspace_id=None))
    second = await _sleep_handler({"seconds": 3600}, ctx=ToolContext(tool_call_id="call_0", session_id="s2", workspace_id=None))

    assert first.event_key == "timer:s1:call_0"
    assert second.event_key == "timer:s2:call_0"


@pytest.mark.asyncio
async def test_a_sleep_without_a_session_keeps_the_key_it_had() -> None:
    """Nothing to scope by (there is no session to wake either): the key is unchanged."""
    y = await _sleep_handler({"seconds": 60}, ctx=ToolContext(tool_call_id="call_0", session_id=None, workspace_id=None))

    assert y.event_key == "timer:call_0"


# ---- the flip -------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_timer_fire_does_not_wake_a_sleep_whose_deadline_is_ahead() -> None:
    did, row = await _flip(_sleeping("s2", until=timedelta(hours=1)), {})

    assert did is False
    assert row.parked_status == "parked" and "resume_event_payload" not in (row.parked_state or {})
    assert metrics.session_wake_stale_refused_total._value.get() == 1


@pytest.mark.asyncio
async def test_a_timer_fire_wakes_a_sleep_whose_deadline_has_passed() -> None:
    did, row = await _flip(_sleeping("s1", until=-timedelta(seconds=1)), {})

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_timer_fire_tolerates_a_little_clock_skew() -> None:
    did, row = await _flip(_sleeping("s1", until=timedelta(seconds=2)), {})

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_timer_fire_for_a_park_with_no_deadline_is_judged_as_before() -> None:
    did, row = await _flip(_sleeping("s1", until=None), {})

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_cancel_wakes_a_sleep_whatever_its_deadline() -> None:
    """The operator cancelling a sleep is a decision, not a clock: it applies at once."""
    did, row = await _flip(_sleeping("s1", until=timedelta(hours=1)), make_cancelled_payload(reason="operator"))

    assert did is True and row.parked_status == "resumable"


# ---- the whole path: the real scheduler's publish, then the sink's flip ---------------------------------------------------------------------------


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key: str, payload: dict | None = None) -> None:
        self.published.append((event_key, payload or {}))


@pytest.mark.asyncio
async def test_one_sessions_timer_does_not_wake_another_sessions_sleep_that_shares_its_key() -> None:
    """The reviewer's scenario: both sessions parked on the shared key before this release, s1 due, s2 an hour away. The real ``TimerScheduler`` publishes the
    shared key once s1 is due; the sink flips every row parked on it."""
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(_sleeping("s1", until=-timedelta(seconds=1)))
    await storage.create(_sleeping("s2", until=timedelta(hours=1)))
    bus = _RecordingBus()

    await TimerScheduler(bus=bus, session_storage=storage)._tick()
    for event_key, payload in bus.published:
        await flip_sessions_parked_on(event_key, payload, session_storage=storage, engine=None)

    assert (await storage.get("s1")).parked_status == "resumable"
    assert (await storage.get("s2")).parked_status == "parked", "s2 slept for an hour; s1's timer is not its timer"


@pytest.mark.asyncio
async def test_a_timer_fire_redelivered_after_the_session_slept_again_does_not_wake_the_new_sleep() -> None:
    """One session, one key: it slept on ``call_0``, the fire was published, the session woke and slept again on ``call_0`` for an hour; the first fire is
    delivered a second time (the dispatcher is at-least-once)."""
    key = "timer:s1:call_0"
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(_sleeping("s1", until=timedelta(hours=1), key=key))

    flipped = await flip_sessions_parked_on(key, {}, session_storage=storage, engine=None)

    assert flipped == 0 and (await storage.get("s1")).parked_status == "parked"
