"""S7 section 4: turn counters keyed by binding_ref, never by session id."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import _binding_ref, _observe_turn


@pytest.fixture(autouse=True)
def _reset_metrics():
    import primer.observability.metrics as m
    m.reset_for_test()
    yield
    m.reset_for_test()


def _session(binding) -> WorkspaceSession:
    return WorkspaceSession(
        id="sess-1",
        workspace_id="ws-1",
        binding=binding,
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )


def test_binding_ref_is_the_agent_id():
    assert _binding_ref(_session(AgentSessionBinding(agent_id="ag-7"))) == "ag-7"


def test_binding_ref_is_the_graph_id():
    assert _binding_ref(_session(GraphSessionBinding(graph_id="gr-2"))) == "gr-2"


def test_observe_turn_counts_and_times():
    import primer.observability.metrics as m
    started = datetime.now(timezone.utc) - timedelta(seconds=3)
    _observe_turn(_session(AgentSessionBinding(agent_id="ag-7")), "completed", started)
    assert m.turns_total.labels("ag-7", "completed")._value.get() == 1.0
    assert m.turn_duration_seconds.labels("ag-7", "completed")._sum.get() >= 2.5


def test_every_terminal_status_is_recordable():
    import primer.observability.metrics as m
    sess = _session(AgentSessionBinding(agent_id="ag-7"))
    started = datetime.now(timezone.utc)
    for status in ("completed", "failed", "cancelled", "parked"):
        _observe_turn(sess, status, started)
    seen = {
        s.labels["status"]
        for metric in m.turns_total.collect()
        for s in metric.samples
        if s.name == "turns_total"
    }
    assert seen == {"completed", "failed", "cancelled", "parked"}


def test_no_session_id_label_leaks():
    import primer.observability.metrics as m
    _observe_turn(_session(AgentSessionBinding(agent_id="ag-7")), "completed",
                  datetime.now(timezone.utc))
    for metric in m.turns_total.collect():
        for s in metric.samples:
            assert "session_id" not in s.labels
