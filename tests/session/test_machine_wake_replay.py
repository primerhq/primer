"""A redelivered machine wake does not decide a LATER park under the same key (security ticket 01a1208d, from the #684 review; follows C-033 PR 4).

PR 4 made a HUMAN decision's wake name the gate it decided. The wakes machines publish carry no gate, and are delivered by event key alone and at least
once, so one redelivered after the session re-parked under the same provider id flipped the NEW park:

* a TIMEOUT marker (``__yield_timeout__``, published by the timeout sweeper for a park past its deadline) timed out the later gate the moment it arrived,
  although that gate's own deadline was still ahead. The flip now refuses a timeout marker for a row whose ``parked_until`` is still ahead (a few seconds of
  clock skew between the sweeper's node and the flipping one are tolerated; a park with no deadline keeps today's behaviour);
* a wake published by a producer that read the park (a trigger fire through ``respond_to_yield``, an external tool result) names the park it read,
  ``__yield_parked_at__``; the flip refuses one whose park is not the one pending (a single park only: a graph park gets a fresh ``parked_at`` whenever a
  sibling is resolved, so for a graph the stamp says nothing and is not judged).

Both keys are primer-internal (``__yield_`` prefix): the resume classification strips them before any hook sees the payload.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

import primer.observability.metrics as metrics
from primer.model.workspace_session import WorkspaceSession
from primer.session.yields import durably_mark_session_resumable, flip_sessions_parked_on
from primer.worker.yield_runtime import make_timeout_payload
from tests.api.test_gate_fence import G2, _approval_session, _graph_two_gate_session
from tests.conftest import _FakeStorageProvider

PARK_KEY = "__yield_parked_at__"
KEY = "tool_approval:s:call_0"


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


def _refused() -> float:
    return metrics.session_wake_stale_refused_total._value.get()


def _parked(*, until: timedelta | None, parked_at: datetime | None = None) -> WorkspaceSession:
    row = _approval_session(session_id="s", tool_call_id="call_0", gate_id=G2)
    row.parked_until = None if until is None else datetime.now(UTC) + until
    if parked_at is not None:
        row.parked_at = parked_at
    return row


async def _flip(row: WorkspaceSession, payload: dict, event_key: str = KEY):
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(row)
    did = await durably_mark_session_resumable(row, event_key=event_key, payload=payload, session_storage=storage, engine=None)
    return did, await storage.get(row.id)


# ---- the timeout marker ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_timeout_marker_does_not_time_out_a_park_whose_deadline_is_ahead(caplog) -> None:
    """The reviewer's probe: the marker of round 1's expired gate, redelivered after the session re-parked under the same key (a fresh deadline)."""
    with caplog.at_level(logging.WARNING):
        did, row = await _flip(_parked(until=timedelta(seconds=600)), make_timeout_payload())

    assert did is False
    assert row.parked_status == "parked" and "resume_event_payload" not in (row.parked_state or {})
    assert _refused() == 1
    assert any("s" in r.getMessage() and "deadline" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_the_probes_own_call_through_the_flip_sink_changes_nothing() -> None:
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(_parked(until=timedelta(seconds=600)))

    flipped = await flip_sessions_parked_on(KEY, make_timeout_payload(), session_storage=storage, engine=None)

    assert flipped == 0 and (await storage.get("s")).parked_status == "parked"


@pytest.mark.asyncio
async def test_a_timeout_marker_times_out_a_park_whose_deadline_has_passed() -> None:
    did, row = await _flip(_parked(until=-timedelta(seconds=1)), make_timeout_payload())

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_timeout_marker_tolerates_a_little_clock_skew() -> None:
    """The sweeper selected the row on ITS clock; the flipping node's clock may be a few seconds behind."""
    did, row = await _flip(_parked(until=timedelta(seconds=2)), make_timeout_payload())

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_timeout_marker_for_a_park_with_no_deadline_is_judged_as_before() -> None:
    did, row = await _flip(_parked(until=None), make_timeout_payload())

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_wake_that_is_not_a_timeout_marker_ignores_the_deadline() -> None:
    did, row = await _flip(_parked(until=timedelta(seconds=600)), {"decision": "approved"})

    assert did is True and row.parked_status == "resumable"


# ---- the park a producer read -------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wake_naming_the_pending_park_flips_it() -> None:
    row = _parked(until=None)
    did, after = await _flip(row, {"result": "x", PARK_KEY: row.parked_at.isoformat()})

    assert did is True and after.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_wake_that_is_not_refused_is_not_counted() -> None:
    row = _parked(until=-timedelta(seconds=1))

    await _flip(row, make_timeout_payload())
    await _flip(_parked(until=None, parked_at=row.parked_at), {"result": "x", PARK_KEY: row.parked_at.isoformat()})

    assert _refused() == 0


@pytest.mark.asyncio
async def test_a_wake_naming_an_earlier_park_is_refused_and_writes_nothing(caplog) -> None:
    earlier = datetime.now(UTC) - timedelta(minutes=5)
    with caplog.at_level(logging.WARNING):
        did, row = await _flip(_parked(until=None), {"result": "x", PARK_KEY: earlier.isoformat()})

    assert did is False
    assert row.parked_status == "parked" and "resume_event_payload" not in (row.parked_state or {})
    assert _refused() == 1
    assert any("earlier park" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_a_wake_naming_no_park_is_judged_as_before() -> None:
    """Written before this release, or by a producer that does not know the park."""
    did, row = await _flip(_parked(until=None), {"result": "x"})

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_an_unreadable_park_stamp_is_judged_as_before() -> None:
    did, row = await _flip(_parked(until=None), {"result": "x", PARK_KEY: "not a timestamp"})

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_graph_parks_stamp_is_not_judged() -> None:
    """Resolving one sibling re-parks the graph on the others with a FRESH ``parked_at``, so a wake that was right when its producer read the row would be refused."""
    row = _graph_two_gate_session(session_id="s", same_raw_id=True)
    earlier = row.parked_at - timedelta(minutes=5)
    key = "tool_approval:s:worker[1]:dup"

    did, after = await _flip(row, {"result": "x", PARK_KEY: earlier.isoformat()}, event_key=key)

    assert did is True and after.parked_status == "resumable"


# ---- the producers ------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respond_to_yield_names_the_park_it_read() -> None:
    from primer.session.yields import RespondToYieldDeps, respond_to_yield

    sp = _FakeStorageProvider()
    row = _parked(until=None)
    await sp.get_storage(WorkspaceSession).create(row)
    published: list[tuple[str, dict]] = []

    class _Bus:
        async def publish(self, key, payload):
            published.append((key, payload))

    await respond_to_yield(session_id="s", tool_call_id="call_0", result={"ok": True}, deps=RespondToYieldDeps(storage_provider=sp, event_bus=_Bus()))

    [(key, payload)] = published
    assert key == KEY and payload["ok"] is True
    assert datetime.fromisoformat(payload[PARK_KEY]) == row.parked_at


@pytest.mark.asyncio
async def test_a_trigger_result_redelivered_after_a_re_park_is_refused() -> None:
    """End to end for a producer that read the park: the payload it published, delivered again once the session parked afresh under the same key."""
    from primer.session.yields import RespondToYieldDeps, respond_to_yield

    sp = _FakeStorageProvider()
    first = _parked(until=None)
    storage = sp.get_storage(WorkspaceSession)
    await storage.create(first)
    published: list[dict] = []

    class _Bus:
        async def publish(self, key, payload):
            published.append(payload)

    await respond_to_yield(session_id="s", tool_call_id="call_0", result={"ok": True}, deps=RespondToYieldDeps(storage_provider=sp, event_bus=_Bus()))
    await storage.update(first.model_copy(update={"parked_at": first.parked_at + timedelta(minutes=3)}))        # the session resumed and parked again

    flipped = await flip_sessions_parked_on(KEY, published[0], session_storage=storage, engine=None)

    assert flipped == 0 and (await storage.get("s")).parked_status == "parked"


def test_the_machine_keys_are_stripped_from_a_real_reply_before_a_hook_sees_it() -> None:
    from primer.worker.yield_runtime import classify_marker_payload

    out = classify_marker_payload({"response": "x", PARK_KEY: datetime.now(UTC).isoformat()}, parked_at=datetime.now(UTC)).payload

    assert out == {"response": "x"}


@pytest.mark.asyncio
async def test_an_external_result_names_the_park_it_read(tmp_path) -> None:
    """``apply_tool_results`` wakes the park twice (the durable flip, then the replayable event): both carry the park, and the flip it makes itself is not refused."""
    from primer.model.external_tool import ExternalToolCall, ExternalToolResultIn
    from primer.model.provider import SqliteConfig
    from primer.session.external_tools import apply_tool_results
    from primer.storage.sqlite import SqliteStorageProvider
    from tests.session.test_external_row_guarded_writes import _call, _parked_session

    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "ext.sqlite"))
    await provider.initialize()
    try:
        sessions, calls = provider.get_storage(WorkspaceSession), provider.get_storage(ExternalToolCall)
        await _parked_session(provider)
        await calls.create(_call("etool-fixed-1", "tc-1"))
        published: list[tuple[str, dict]] = []

        class _Bus:
            async def publish(self, key, payload):
                published.append((key, payload))

        session = await sessions.get("sess-1")
        applied = await apply_tool_results(
            session, [ExternalToolResultIn(tool_call_id="tc-1", result={"customer": "c1"})],
            call_storage=calls, session_storage=sessions, engine=None, event_bus=_Bus(),
        )

        assert applied == 1
        [(_key, payload)] = published
        assert datetime.fromisoformat(payload[PARK_KEY]) == session.parked_at
        assert (await sessions.get("sess-1")).parked_status == "resumable", "the producer's own durable flip landed"
    finally:
        await provider.aclose()
