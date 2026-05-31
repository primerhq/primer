"""WorkspaceSession gains an `ended_detail: str | None` field that carries
graph-specific failure codes without expanding the `ended_reason` Literal."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


def _make_session(**overrides):
    base = dict(
        id="sess-1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.ENDED,
        created_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        ended_reason="failed",
    )
    base.update(overrides)
    return WorkspaceSession(**base)


def test_ended_detail_defaults_to_none() -> None:
    s = _make_session()
    assert s.ended_detail is None


def test_ended_detail_accepts_arbitrary_string() -> None:
    s = _make_session(ended_detail="end_output_invalid")
    assert s.ended_detail == "end_output_invalid"


def test_ended_detail_round_trips_through_json() -> None:
    s = _make_session(ended_detail="routing_failed")
    payload = s.model_dump_json()
    rev = WorkspaceSession.model_validate_json(payload)
    assert rev.ended_detail == "routing_failed"
