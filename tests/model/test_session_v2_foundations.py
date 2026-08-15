"""S1 P1 foundations: mutable bindings, epochs, new record kinds.

Spec: docs/superpowers/ux-revamp/02-s1-design.md sections 3 and 4.

A session is an agent-independent workstream: its binding is a mutable
pointer carrying an epoch, and its history gains the record kinds chat
had (reasoning / external_tool_call / agent_marker) plus the structural
rewind_marker the replay rule consumes. Follow-up steers that arrive
while a turn is running become seq-less pending rows, realized only at
the drain checkpoint, so they can never collide with the in-flight
turn's seq allocation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    PendingSessionMessage,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)


def _session(**overrides) -> WorkspaceSession:
    base = {
        "id": "sess-v2-1",
        "workspace_id": "ws-1",
        "binding": AgentSessionBinding(agent_id="agent-1"),
        "status": SessionStatus.CREATED,
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return WorkspaceSession(**base)


class TestRecordKinds:
    """The kinds a v2 transcript needs that only chats had."""

    @pytest.mark.parametrize(
        ("member", "value"),
        [
            ("REASONING", "reasoning"),
            ("EXTERNAL_TOOL_CALL", "external_tool_call"),
            ("AGENT_MARKER", "agent_marker"),
            ("REWIND_MARKER", "rewind_marker"),
        ],
    )
    def test_kind_exists(self, member: str, value: str) -> None:
        assert getattr(SessionMessageKind, member).value == value

    def test_pre_existing_kinds_survive(self) -> None:
        """The additions must not disturb the kinds already written."""
        assert SessionMessageKind.USER_INPUT.value == "user_input"
        assert SessionMessageKind.COMPACTION_MARKER.value == "compaction_marker"


class TestBindingEpoch:
    """Epochs fence writes across a switch (spec section 6)."""

    def test_defaults_to_zero(self) -> None:
        assert _session().binding_epoch == 0

    def test_is_carried_on_the_row(self) -> None:
        assert _session(binding_epoch=7).binding_epoch == 7


class TestGraphBindingProfileOverride:
    """Both binding kinds carry the per-run profile override.

    Before this, only agent bindings could pin a model, so a graph
    session had no way to express 'run this graph against that profile'
    even though graph agent nodes resolve profiles individually.
    """

    def test_graph_binding_accepts_profile_id(self) -> None:
        binding = GraphSessionBinding(graph_id="graph-1", profile_id="prof-1")
        assert binding.profile_id == "prof-1"

    def test_graph_binding_profile_is_optional(self) -> None:
        assert GraphSessionBinding(graph_id="graph-1").profile_id is None

    def test_agent_binding_keeps_its_override(self) -> None:
        binding = AgentSessionBinding(agent_id="agent-1", profile_id="prof-2")
        assert binding.profile_id == "prof-2"


class TestPendingSessionMessage:
    """Seq-less follow-ups (spec sections 3 and 4).

    The row deliberately carries no seq: assigning one at receipt is
    what collided with the in-flight turn's assistant_token seqs on the
    chat surface, so the drain assigns it at the checkpoint instead.
    """

    def test_carries_session_scope_and_no_seq(self) -> None:
        now = datetime.now(UTC)
        pending = PendingSessionMessage(
            id="sess-1:pending:0001",
            session_id="sess-1",
            parts=[{"type": "text", "text": "follow up"}],
            enqueued_at=now,
            created_at=now,
        )
        assert pending.session_id == "sess-1"
        assert pending.parts[0]["text"] == "follow up"
        assert not hasattr(pending, "seq")

    def test_attribution_and_client_msg_id_are_optional(self) -> None:
        now = datetime.now(UTC)
        pending = PendingSessionMessage(
            id="sess-1:pending:0002",
            session_id="sess-1",
            enqueued_at=now,
            created_at=now,
        )
        assert pending.attribution is None
        assert pending.client_msg_id is None
        assert pending.parts == []
