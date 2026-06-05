"""Tests for the TurnLogEvent discriminated union and TurnLogRecord
storage entity."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.api.errors import ProblemDetails
from primer.model.turn_log import (
    TurnLogCancelled,
    TurnLogCompleted,
    TurnLogFailed,
    TurnLogKind,
    TurnLogRecord,
    TurnLogResumed,
    TurnLogStarted,
    TurnLogSuperstepEnded,
    TurnLogSuperstepStarted,
    TurnLogYielded,
    parse_turn_log_event,
)


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class TestStarted:
    def test_round_trip(self):
        ev = TurnLogStarted(
            seq=1,
            ts=_utc("2026-06-05T10:00:00"),
            model="anthropic/claude-3.5-sonnet",
            input_message_count=4,
        )
        dumped = ev.model_dump(mode="json")
        assert dumped["kind"] == "started"
        re_parsed = parse_turn_log_event(dumped)
        assert isinstance(re_parsed, TurnLogStarted)
        assert re_parsed.model == "anthropic/claude-3.5-sonnet"


class TestCompleted:
    def test_round_trip(self):
        ev = TurnLogCompleted(
            seq=2,
            ts=_utc("2026-06-05T10:00:05"),
            duration_ms=4218,
            input_tokens=1024,
            output_tokens=312,
            finish_reason="stop",
        )
        re_parsed = parse_turn_log_event(ev.model_dump(mode="json"))
        assert isinstance(re_parsed, TurnLogCompleted)
        assert re_parsed.duration_ms == 4218


class TestFailed:
    def test_carries_problem_details(self):
        pd = ProblemDetails(
            type="/errors/network-error",
            title="Network Error",
            status=504,
            detail="Connection reset by peer",
        )
        ev = TurnLogFailed(
            seq=3,
            ts=_utc("2026-06-05T10:00:10"),
            duration_ms=1106,
            error=pd,
        )
        re_parsed = parse_turn_log_event(ev.model_dump(mode="json"))
        assert isinstance(re_parsed, TurnLogFailed)
        assert re_parsed.error.status == 504
        assert re_parsed.error.title == "Network Error"


class TestYielded:
    def test_round_trip(self):
        ev = TurnLogYielded(
            seq=4,
            ts=_utc("2026-06-05T10:00:11"),
            yield_kind="ask_user",
            event_key="ask_user:sess-x:tcid-y",
        )
        re_parsed = parse_turn_log_event(ev.model_dump(mode="json"))
        assert isinstance(re_parsed, TurnLogYielded)
        assert re_parsed.yield_kind == "ask_user"

    def test_invalid_yield_kind_rejected(self):
        with pytest.raises(ValidationError):
            TurnLogYielded(
                seq=4,
                ts=_utc("2026-06-05T10:00:11"),
                yield_kind="not_a_real_kind",  # type: ignore[arg-type]
                event_key="x",
            )


class TestResumed:
    def test_round_trip(self):
        ev = TurnLogResumed(
            seq=5,
            ts=_utc("2026-06-05T11:00:00"),
            wait_ms=3540000,
            resume_kind="event_fired",
        )
        re_parsed = parse_turn_log_event(ev.model_dump(mode="json"))
        assert isinstance(re_parsed, TurnLogResumed)
        assert re_parsed.wait_ms == 3540000


class TestCancelled:
    def test_round_trip(self):
        ev = TurnLogCancelled(
            seq=6,
            ts=_utc("2026-06-05T10:00:20"),
            reason="operator interrupt",
        )
        re_parsed = parse_turn_log_event(ev.model_dump(mode="json"))
        assert isinstance(re_parsed, TurnLogCancelled)


class TestGraphExtras:
    def test_node_id_iteration_superstep_carry(self):
        ev = TurnLogStarted(
            seq=1,
            ts=_utc("2026-06-05T10:00:00"),
            model="x",
            input_message_count=1,
            node_id="researcher",
            iteration=3,
            superstep_id="ss-3-abc",
        )
        re_parsed = parse_turn_log_event(ev.model_dump(mode="json"))
        assert re_parsed.node_id == "researcher"
        assert re_parsed.iteration == 3
        assert re_parsed.superstep_id == "ss-3-abc"


class TestSuperstepEvents:
    def test_superstep_started(self):
        ev = TurnLogSuperstepStarted(
            seq=10,
            ts=_utc("2026-06-05T10:00:00"),
            iteration=2,
            superstep_id="ss-2-xyz",
            ready_node_ids=["a", "b", "c"],
        )
        re_parsed = parse_turn_log_event(ev.model_dump(mode="json"))
        assert isinstance(re_parsed, TurnLogSuperstepStarted)
        assert re_parsed.ready_node_ids == ["a", "b", "c"]

    def test_superstep_ended(self):
        ev = TurnLogSuperstepEnded(
            seq=11,
            ts=_utc("2026-06-05T10:00:05"),
            iteration=2,
            superstep_id="ss-2-xyz",
            completed_node_ids=["a", "c"],
            failed_node_ids=["b"],
            duration_ms=5012,
        )
        re_parsed = parse_turn_log_event(ev.model_dump(mode="json"))
        assert isinstance(re_parsed, TurnLogSuperstepEnded)
        assert re_parsed.failed_node_ids == ["b"]


class TestTurnLogRecord:
    def test_storage_entity_round_trip(self):
        rec = TurnLogRecord(
            id="tlr-001",
            run_id="run-xyz",
            node_id="researcher",
            seq=1,
            kind=TurnLogKind.STARTED,
            iteration=0,
            superstep_id=None,
            payload={
                "model": "anthropic/claude-3.5-sonnet",
                "input_message_count": 1,
            },
            created_at=_utc("2026-06-05T10:00:00"),
        )
        dumped = rec.model_dump(mode="json")
        re_parsed = TurnLogRecord.model_validate(dumped)
        assert re_parsed.run_id == "run-xyz"
        assert re_parsed.payload["model"] == "anthropic/claude-3.5-sonnet"

    def test_record_with_null_node_id(self):
        rec = TurnLogRecord(
            id="tlr-002",
            run_id="run-xyz",
            node_id=None,
            seq=1,
            kind=TurnLogKind.SUPERSTEP_STARTED,
            iteration=0,
            superstep_id="ss-0-a",
            payload={"ready_node_ids": ["a", "b"]},
            created_at=_utc("2026-06-05T10:00:00"),
        )
        assert rec.node_id is None
        assert rec.kind == TurnLogKind.SUPERSTEP_STARTED
