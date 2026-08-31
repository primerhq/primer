"""The session read surface (S1 P4 Task 23, amendment M14).

Lands SessionInfo.binding, the last of the three fields errata E6
flagged as declared but never built.

The envelope is FLAT on purpose: SessionDetail subclasses the row, so
every existing field stays a literal sibling and no client that reads
WorkspaceSession today breaks. A wrapper would have re-nested the row
and forced every existing reader to change.
"""

from datetime import UTC, datetime

from primer.api.routers.sessions import SessionDetail
from primer.model.workspace_session import (
    AgentSessionBinding,
    PendingSessionMessage,
    SessionInfo,
    SessionStatus,
    WorkspaceSession,
)


def _row(**kw):
    base = {
        "id": "sess-1", "workspace_id": "ws-1",
        "binding": AgentSessionBinding(agent_id="agent-a"),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return WorkspaceSession(**base)


def _info(**kw):
    base = {
        "session_id": "sess-1", "agent_id": "agent-a",
        "workspace_id": "ws-1", "status": SessionStatus.WAITING,
        "started_at": datetime.now(UTC),
        "last_activity_at": datetime.now(UTC),
    }
    base.update(kw)
    return SessionInfo(**base)


class TestSessionInfoBinding:
    def test_defaults_to_none(self):
        assert _info().binding is None

    def test_carries_the_row_shape_without_rewriting_agent_id(self):
        """agent_id becomes display-legacy: the binding is the truth."""
        switched = _info().model_copy(update={"binding": {
            "kind": "agent", "agent_id": "agent-b",
            "profile_id": None, "binding_epoch": 2,
        }})
        assert switched.binding["kind"] == "agent"
        assert switched.binding["agent_id"] == "agent-b"
        assert switched.agent_id == "agent-a"

    def test_round_trips_through_json(self):
        switched = _info().model_copy(update={"binding": {
            "kind": "graph", "graph_id": "g-1", "binding_epoch": 1,
        }})
        again = SessionInfo.model_validate(switched.model_dump(mode="json"))
        assert again.binding["graph_id"] == "g-1"


class TestSessionDetail:
    def test_is_flat_and_adds_only_pending_messages(self):
        now = datetime.now(UTC)
        detail = SessionDetail(
            **_row(binding_epoch=2, turn_status="idle").model_dump(),
            pending_messages=[PendingSessionMessage(
                id="sess-1:pending:x", session_id="sess-1",
                parts=[{"type": "text", "text": "follow-up"}],
                enqueued_at=now, created_at=now,
            )],
        )
        dumped = detail.model_dump(mode="json")

        # Flat: the row's fields are siblings, never nested under a key.
        assert "session" not in dumped
        assert dumped["id"] == "sess-1"
        assert dumped["binding"]["agent_id"] == "agent-a"
        assert dumped["binding_epoch"] == 2
        assert dumped["turn_status"] == "idle"

        # parts, not a second "content" projection that would drift the
        # moment a steer carries an attachment.
        assert dumped["pending_messages"][0]["parts"][0]["text"] == "follow-up"
        assert "content" not in dumped["pending_messages"][0]

    def test_defaults_to_no_pending_messages(self):
        assert SessionDetail(**_row().model_dump()).pending_messages == []

    def test_defaults_usage_and_context_length_to_none(self):
        """01a052a5 item 2: both derived server-side per request (see
        get_session_by_id), never stored on the row - a bare construction
        with no override carries no opinion either way."""
        detail = SessionDetail(**_row().model_dump())
        assert detail.usage is None
        assert detail.context_length is None

    def test_usage_and_context_length_are_flat_siblings_too(self):
        detail = SessionDetail(
            **_row().model_dump(),
            usage={"total_input_tokens": 1000, "total_output_tokens": 500},
            context_length=64_000,
        )
        dumped = detail.model_dump(mode="json")
        assert dumped["usage"]["total_input_tokens"] == 1000
        assert dumped["context_length"] == 64_000

    def test_every_row_field_survives_the_subclass(self):
        """The reason for subclassing: no existing reader breaks."""
        row = _row(binding_epoch=3, last_seq=9, next_unprocessed_seq=10)
        detail = SessionDetail(**row.model_dump())
        for field in row.model_dump():
            assert getattr(detail, field) == getattr(row, field)
