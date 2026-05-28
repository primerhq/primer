"""Tests for matrix.model.session."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from matrix.model.session import (
    AgentBinding,
    Instruction,
    SessionInfo,
    SessionStatus,
    WaitingState,
)


# ---- SessionStatus -------------------------------------------------------


class TestSessionStatus:
    def test_enum_values(self) -> None:
        assert SessionStatus.CREATED.value == "created"
        assert SessionStatus.RUNNING.value == "running"
        assert SessionStatus.WAITING.value == "waiting"
        assert SessionStatus.PAUSED.value == "paused"
        assert SessionStatus.ENDED.value == "ended"

    def test_member_count(self) -> None:
        # CREATED is the pre-execution state introduced when sessions
        # became background-executed (see
        # docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md).
        assert {s.value for s in SessionStatus} == {
            "created",
            "running",
            "waiting",
            "paused",
            "ended",
        }


# ---- AgentBinding --------------------------------------------------------


class TestAgentBinding:
    def test_minimal_construction_empty_tool_list(self) -> None:
        ab = AgentBinding(agent_id="researcher", agent_name="Research Agent")
        assert ab.agent_id == "researcher"
        assert ab.agent_name == "Research Agent"
        assert ab.registered_tool_ids == []

    def test_with_registered_tools(self) -> None:
        ab = AgentBinding(
            agent_id="researcher",
            agent_name="Research Agent",
            registered_tool_ids=["find_tool", "call_tool", "spawn"],
        )
        assert ab.registered_tool_ids == ["find_tool", "call_tool", "spawn"]

    def test_empty_agent_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentBinding(agent_id="", agent_name="x")

    def test_empty_agent_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentBinding(agent_id="a", agent_name="")

    def test_round_trip_through_json(self) -> None:
        ab = AgentBinding(
            agent_id="r",
            agent_name="R",
            registered_tool_ids=["t1", "t2"],
        )
        parsed = AgentBinding.model_validate_json(ab.model_dump_json())
        assert parsed == ab


# ---- SessionInfo ---------------------------------------------------------


class TestSessionInfo:
    def test_minimal_running_session(self) -> None:
        now = datetime.now(timezone.utc)
        info = SessionInfo(
            session_id="sess-1",
            agent_id="researcher",
            workspace_id="ws-1",
            status=SessionStatus.RUNNING,
            started_at=now,
            last_activity_at=now,
        )
        assert info.status == SessionStatus.RUNNING
        assert info.ended_reason is None
        assert info.parent_session_id is None
        assert info.ended_at is None
        assert info.initial_instructions is None

    def test_full_ended_session(self) -> None:
        started = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 5, 2, 10, 30, 0, tzinfo=timezone.utc)
        info = SessionInfo(
            session_id="sess-2",
            agent_id="researcher",
            workspace_id="ws-1",
            status=SessionStatus.ENDED,
            ended_reason="completed",
            parent_session_id="sess-1",
            started_at=started,
            last_activity_at=ended,
            ended_at=ended,
            initial_instructions="Find the slowest test in the repo.",
        )
        assert info.ended_reason == "completed"
        assert info.parent_session_id == "sess-1"
        assert info.ended_at == ended

    def test_status_must_be_valid_enum(self) -> None:
        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            SessionInfo(
                session_id="sess",
                agent_id="a",
                workspace_id="w",
                status="bogus",  # type: ignore[arg-type]
                started_at=now,
                last_activity_at=now,
            )

    def test_ended_reason_must_be_valid_literal(self) -> None:
        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            SessionInfo(
                session_id="sess",
                agent_id="a",
                workspace_id="w",
                status=SessionStatus.ENDED,
                ended_reason="kaboom",  # type: ignore[arg-type]
                started_at=now,
                last_activity_at=now,
            )

    def test_round_trip_through_json(self) -> None:
        now = datetime.now(timezone.utc)
        info = SessionInfo(
            session_id="sess-1",
            agent_id="r",
            workspace_id="w",
            status=SessionStatus.WAITING,
            started_at=now,
            last_activity_at=now,
            initial_instructions="hi",
        )
        parsed = SessionInfo.model_validate_json(info.model_dump_json())
        assert parsed == info


# ---- Instruction ---------------------------------------------------------


class TestInstruction:
    def test_construction(self) -> None:
        now = datetime.now(timezone.utc)
        ins = Instruction(
            instruction_id="ins-1",
            session_id="sess-1",
            content="Also include qdrant",
            queued_at=now,
        )
        assert ins.instruction_id == "ins-1"
        assert ins.session_id == "sess-1"
        assert ins.content == "Also include qdrant"

    def test_empty_content_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Instruction(
                instruction_id="ins-1",
                session_id="sess-1",
                content="",
                queued_at=datetime.now(timezone.utc),
            )

    def test_empty_session_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Instruction(
                instruction_id="ins-1",
                session_id="",
                content="x",
                queued_at=datetime.now(timezone.utc),
            )


# ---- WaitingState (discriminated union) ----------------------------------

# Use a TypeAdapter because WaitingState is a typing alias
# (Annotated[Union[...], Field(discriminator=...)]), not a class.
_waiting = TypeAdapter(WaitingState)


class TestWaitingStateDiscriminatedUnion:
    def test_user_input_round_trip(self) -> None:
        now = datetime.now(timezone.utc)
        payload = {
            "kind": "user_input",
            "prompt": "What's the deadline?",
            "queued_at": now.isoformat(),
        }
        parsed = _waiting.validate_python(payload)
        assert parsed.kind == "user_input"
        assert parsed.prompt == "What's the deadline?"

        dumped = _waiting.dump_python(parsed, mode="json")
        re_parsed = _waiting.validate_python(dumped)
        assert re_parsed == parsed

    def test_tool_approval_round_trip(self) -> None:
        now = datetime.now(timezone.utc)
        payload = {
            "kind": "tool_approval",
            "tool_id": "exec",
            "arguments": {"command": "rm -rf /tmp/scratch"},
            "rationale": "Cleaning up scratch directory",
            "queued_at": now.isoformat(),
        }
        parsed = _waiting.validate_python(payload)
        assert parsed.kind == "tool_approval"
        assert parsed.tool_id == "exec"
        assert parsed.arguments == {"command": "rm -rf /tmp/scratch"}
        assert parsed.rationale == "Cleaning up scratch directory"

        dumped = _waiting.dump_python(parsed, mode="json")
        re_parsed = _waiting.validate_python(dumped)
        assert re_parsed == parsed

    def test_tool_approval_optional_rationale(self) -> None:
        now = datetime.now(timezone.utc)
        parsed = _waiting.validate_python(
            {
                "kind": "tool_approval",
                "tool_id": "write",
                "queued_at": now.isoformat(),
            }
        )
        assert parsed.kind == "tool_approval"
        assert parsed.rationale is None
        assert parsed.arguments == {}

    def test_unknown_kind_rejected_by_discriminator(self) -> None:
        with pytest.raises(ValidationError):
            _waiting.validate_python(
                {
                    "kind": "carrier_pigeon",
                    "details": "...",
                }
            )

    def test_user_input_missing_prompt_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _waiting.validate_python(
                {
                    "kind": "user_input",
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }
            )

    def test_user_input_empty_prompt_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _waiting.validate_python(
                {
                    "kind": "user_input",
                    "prompt": "",
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }
            )

    def test_tool_approval_missing_tool_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _waiting.validate_python(
                {
                    "kind": "tool_approval",
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }
            )


# ---- Session entity (scheduler-visible) ----------------------------------


from matrix.model.session import (  # noqa: E402
    AgentSessionBinding,
    GraphSessionBinding,
    Session,
)


class TestSessionEntity:
    def test_agent_session_binding_kind_is_literal(self):
        b = AgentSessionBinding(agent_id="ag-1")
        assert b.kind == "agent"

    def test_graph_session_binding_kind_is_literal(self):
        b = GraphSessionBinding(graph_id="gr-1")
        assert b.kind == "graph"

    def test_round_trip_with_agent_binding(self):
        s = Session(
            id="sess-1",
            workspace_id="ws-1",
            binding=AgentSessionBinding(agent_id="ag-1"),
            status=SessionStatus.CREATED,
            created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        again = Session.model_validate(s.model_dump(mode="json"))
        assert again.binding.kind == "agent"
        assert again.turn_no == 0
        assert again.cancel_requested is False
        assert again.pause_requested is False

    def test_round_trip_with_graph_binding(self):
        s = Session(
            id="sess-2",
            workspace_id="ws-1",
            binding=GraphSessionBinding(graph_id="gr-1"),
            status=SessionStatus.CREATED,
            created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        again = Session.model_validate(s.model_dump(mode="json"))
        assert again.binding.kind == "graph"

    def test_binding_discriminator_rejects_unknown_kind(self):
        with pytest.raises(ValidationError):
            Session.model_validate({
                "id": "sess-3",
                "workspace_id": "ws-1",
                "binding": {"kind": "mystery"},
                "status": "created",
                "created_at": "2026-05-10T00:00:00+00:00",
            })
