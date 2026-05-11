"""Tests for the Scheduler ABC + value types in matrix.int.scheduler."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from matrix.int.scheduler import (
    CompleteTurnResult,
    FailureRecord,
    Lease,
    Scheduler,
    WorkerInfo,
)


def test_complete_turn_result_values():
    assert CompleteTurnResult.SUCCESS.value == "success"
    assert CompleteTurnResult.LEASE_LOST.value == "lease_lost"
    assert CompleteTurnResult.TURN_CONFLICT.value == "turn_conflict"


def test_lease_round_trip():
    l = Lease(
        session_id="s",
        worker_id="w",
        expires_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        attempt_count=0,
        turn_no=0,
    )
    assert Lease.model_validate(l.model_dump(mode="json")) == l


def test_worker_info_round_trip():
    w = WorkerInfo(
        id="w",
        host="h",
        pid=1,
        capacity=4,
        started_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        last_heartbeat=datetime(2026, 5, 10, tzinfo=timezone.utc),
        status="active",
    )
    assert WorkerInfo.model_validate(w.model_dump(mode="json")) == w


def test_failure_record_round_trip():
    f = FailureRecord(error_text="boom", attempt_count=2)
    assert FailureRecord.model_validate(f.model_dump(mode="json")) == f


def test_scheduler_is_abstract():
    with pytest.raises(TypeError):
        Scheduler()  # type: ignore[abstract]


def test_scheduler_re_exported_from_matrix_int():
    """Scheduler should be re-exported from matrix.int alongside other ABCs."""
    from matrix import int as matrix_int
    assert hasattr(matrix_int, "Scheduler")
