"""Unit tests for matrix.worker.yield_runtime — park/resume serialisation.

Verifies the ParkedState JSON round-trip, the resume-payload
classifier (real event / timeout / cancelled), and the publisher
helpers. No I/O, no DB, no server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from matrix.model.yield_ import YieldCancelled, YieldTimeout, Yielded
from matrix.worker.yield_runtime import (
    PARKED_STATE_SCHEMA_VERSION,
    ParkedState,
    classify_resume_payload,
    make_cancelled_payload,
    make_timeout_payload,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def t0() -> datetime:
    """A fixed UTC instant; deterministic across all tests in this module."""
    return datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def yielded_sleep() -> Yielded:
    return Yielded(
        tool_name="sleep",
        event_key="timer:tc-1",
        timeout=300.0,
        resume_metadata={"requested_seconds": 300.0},
    )


@pytest.fixture
def parked_state(yielded_sleep: Yielded, t0: datetime) -> ParkedState:
    return ParkedState(
        yielded=yielded_sleep,
        llm_messages=[
            {"role": "user", "content": "wait 5 minutes then continue"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-1",
                        "name": "sleep",
                        "arguments": {"seconds": 300},
                    }
                ],
            },
        ],
        turn_no=3,
        started_at=t0,
    )


# ===========================================================================
# ParkedState serialisation
# ===========================================================================


class TestParkedStateRoundTrip:
    def test_minimal_roundtrip(self, parked_state: ParkedState):
        round_tripped = ParkedState.from_jsonable(parked_state.to_jsonable())
        assert round_tripped == parked_state

    def test_roundtrip_with_resume_event_payload(self, parked_state: ParkedState):
        resumed = ParkedState(
            yielded=parked_state.yielded,
            llm_messages=parked_state.llm_messages,
            turn_no=parked_state.turn_no,
            started_at=parked_state.started_at,
            resume_event_payload={"response": "yes please"},
        )
        round_tripped = ParkedState.from_jsonable(resumed.to_jsonable())
        assert round_tripped == resumed

    def test_unknown_schema_version_raises_loudly(self, parked_state: ParkedState):
        blob = parked_state.to_jsonable()
        blob["schema_version"] = 9999
        with pytest.raises(ValueError, match="schema_version"):
            ParkedState.from_jsonable(blob)

    def test_serialised_blob_is_pure_json(self, parked_state: ParkedState):
        # Every value in the blob must be a JSON-safe primitive
        # (postgres' jsonb_set chokes on Python datetimes etc.) —
        # verifies via a real json.dumps round-trip.
        import json
        blob = parked_state.to_jsonable()
        reparsed = json.loads(json.dumps(blob))
        # Schema fields stay intact through json round-trip.
        assert reparsed["schema_version"] == PARKED_STATE_SCHEMA_VERSION
        assert reparsed["yielded"]["tool_name"] == "sleep"
        assert reparsed["turn_no"] == 3
        # started_at is ISO-8601 string post-serialisation, not a
        # Python datetime.
        assert isinstance(reparsed["started_at"], str)


# ===========================================================================
# classify_resume_payload
# ===========================================================================


class TestClassifyResumePayload:
    def test_real_event_payload_passes_through(
        self, yielded_sleep: Yielded, t0: datetime,
    ):
        state = ParkedState(
            yielded=yielded_sleep,
            llm_messages=[],
            turn_no=1,
            started_at=t0,
            resume_event_payload={"response": "ok", "extra": 42},
        )
        result = classify_resume_payload(
            state, parked_at=t0, now=t0 + timedelta(seconds=10),
        )
        assert result.payload == {"response": "ok", "extra": 42}
        assert result.elapsed_seconds == pytest.approx(10.0)

    def test_timeout_marker_synthesises_YieldTimeout(
        self, yielded_sleep: Yielded, t0: datetime,
    ):
        state = ParkedState(
            yielded=yielded_sleep,
            llm_messages=[],
            turn_no=1,
            started_at=t0,
            resume_event_payload=make_timeout_payload(),
        )
        result = classify_resume_payload(
            state, parked_at=t0, now=t0 + timedelta(seconds=300),
        )
        assert isinstance(result.payload, YieldTimeout)
        assert result.payload.elapsed_seconds == pytest.approx(300.0)
        assert result.elapsed_seconds == pytest.approx(300.0)

    def test_cancelled_marker_synthesises_YieldCancelled(
        self, yielded_sleep: Yielded, t0: datetime,
    ):
        cancelled_at = t0 + timedelta(seconds=42)
        state = ParkedState(
            yielded=yielded_sleep,
            llm_messages=[],
            turn_no=1,
            started_at=t0,
            resume_event_payload=make_cancelled_payload(
                reason="operator changed mind",
                cancelled_at=cancelled_at,
            ),
        )
        result = classify_resume_payload(
            state, parked_at=t0, now=cancelled_at,
        )
        assert isinstance(result.payload, YieldCancelled)
        assert result.payload.reason == "operator changed mind"
        assert result.payload.cancelled_at == cancelled_at
        assert result.payload.elapsed_seconds == pytest.approx(42.0)

    def test_cancelled_with_no_reason(
        self, yielded_sleep: Yielded, t0: datetime,
    ):
        state = ParkedState(
            yielded=yielded_sleep,
            llm_messages=[],
            turn_no=1,
            started_at=t0,
            resume_event_payload=make_cancelled_payload(reason=None),
        )
        result = classify_resume_payload(state, parked_at=t0, now=t0)
        assert isinstance(result.payload, YieldCancelled)
        assert result.payload.reason is None

    def test_missing_resume_event_payload_raises(
        self, yielded_sleep: Yielded, t0: datetime,
    ):
        # Caller invoked classify before publishing the event —
        # programming bug, fail loud.
        state = ParkedState(
            yielded=yielded_sleep,
            llm_messages=[],
            turn_no=1,
            started_at=t0,
            resume_event_payload=None,
        )
        with pytest.raises(ValueError, match="resume_event_payload"):
            classify_resume_payload(state, parked_at=t0)

    def test_real_payload_strips_internal_keys(
        self, yielded_sleep: Yielded, t0: datetime,
    ):
        # If a publisher inadvertently includes one of our marker
        # keys alongside real data, the marker wins. Defensive: we
        # don't want a real "response" payload that also has
        # __yield_timeout__ to silently mis-route. But once the
        # marker is detected, the real payload is dropped — that's
        # documented behaviour.
        state = ParkedState(
            yielded=yielded_sleep,
            llm_messages=[],
            turn_no=1,
            started_at=t0,
            resume_event_payload={
                "__yield_timeout__": True,
                "response": "ignored",
            },
        )
        result = classify_resume_payload(
            state, parked_at=t0, now=t0 + timedelta(seconds=5),
        )
        assert isinstance(result.payload, YieldTimeout)


# ===========================================================================
# Publisher helpers
# ===========================================================================


class TestPublisherHelpers:
    def test_make_timeout_payload_round_trips_to_YieldTimeout(
        self, yielded_sleep: Yielded, t0: datetime,
    ):
        state = ParkedState(
            yielded=yielded_sleep,
            llm_messages=[],
            turn_no=1,
            started_at=t0,
            resume_event_payload=make_timeout_payload(),
        )
        result = classify_resume_payload(state, parked_at=t0, now=t0)
        assert isinstance(result.payload, YieldTimeout)

    def test_make_cancelled_payload_round_trips_to_YieldCancelled(
        self, yielded_sleep: Yielded, t0: datetime,
    ):
        when = t0 + timedelta(seconds=15)
        state = ParkedState(
            yielded=yielded_sleep,
            llm_messages=[],
            turn_no=1,
            started_at=t0,
            resume_event_payload=make_cancelled_payload(
                reason="x", cancelled_at=when,
            ),
        )
        result = classify_resume_payload(state, parked_at=t0, now=when)
        assert isinstance(result.payload, YieldCancelled)
        assert result.payload.reason == "x"
        assert result.payload.cancelled_at == when

    def test_make_cancelled_payload_default_cancelled_at(self):
        # When cancelled_at omitted, defaults to "now" — exact value
        # is hard to test, but it must be tz-aware and parseable.
        payload = make_cancelled_payload(reason="x")
        assert payload["__yield_cancelled__"] is True
        # cancelled_at is an ISO string; parsing it must yield a
        # tz-aware datetime.
        parsed = datetime.fromisoformat(payload["cancelled_at"])
        assert parsed.tzinfo is not None
