"""Persistent and ephemeral structured output on sessions (S1 P2 T13).

Spec decision 8: ephemeral (this turn only) beats session-persistent,
which beats the agent default. This lands the response_format field
errata E6 reassigned here from P1 Task 1.
"""

from datetime import UTC, datetime

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.response_format import (
    EPHEMERAL_KEY,
    effective_response_format,
    pop_ephemeral,
)


def _row(**kw):
    base = {
        "id": "s", "workspace_id": "w",
        "binding": AgentSessionBinding(agent_id="a"),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return WorkspaceSession(**base)


class TestModelField:
    def test_round_trips(self):
        row = _row(response_format={"type": "object"})
        again = WorkspaceSession.model_validate(row.model_dump(mode="json"))
        assert again.response_format == {"type": "object"}

    def test_defaults_to_none(self):
        assert _row().response_format is None

    def test_invalid_schema_is_rejected_at_construction(self):
        with pytest.raises(ValueError):
            _row(response_format={"type": 42})


class TestPrecedence:
    def test_ephemeral_beats_session_beats_agent(self):
        row = _row(
            response_format={"title": "session"},
            metadata={EPHEMERAL_KEY: {"title": "ephemeral"}},
        )
        assert effective_response_format(
            row, agent_default={"title": "agent"},
        ) == {"title": "ephemeral"}

    def test_session_beats_agent_when_no_ephemeral(self):
        row = _row(response_format={"title": "session"})
        assert effective_response_format(
            row, agent_default={"title": "agent"},
        ) == {"title": "session"}

    def test_agent_default_when_session_is_unset(self):
        assert effective_response_format(
            _row(), agent_default={"title": "agent"},
        ) == {"title": "agent"}

    def test_none_everywhere_is_none(self):
        assert effective_response_format(_row()) is None


class TestPopEphemeral:
    def test_pop_returns_and_removes(self):
        row = _row(metadata={EPHEMERAL_KEY: {"title": "once"}})
        assert pop_ephemeral(row) == {"title": "once"}
        assert EPHEMERAL_KEY not in row.metadata

    def test_second_pop_is_none(self):
        """What bounds it to one turn: a retry finds nothing left."""
        row = _row(metadata={EPHEMERAL_KEY: {"title": "once"}})
        pop_ephemeral(row)
        assert pop_ephemeral(row) is None

    def test_pop_on_empty_metadata_is_safe(self):
        assert pop_ephemeral(_row()) is None

    def test_pop_leaves_other_metadata_intact(self):
        row = _row(metadata={EPHEMERAL_KEY: {"a": 1}, "reply_binding": "x"})
        pop_ephemeral(row)
        assert row.metadata == {"reply_binding": "x"}
