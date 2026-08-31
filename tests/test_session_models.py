"""Tests for primer.model.session."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from primer.model.workspace_session import (
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


# ---- WorkspaceSession entity (scheduler-visible) ----------------------------------


from primer.model.workspace_session import (  # noqa: E402
    AgentSessionBinding,
    GraphSessionBinding,
    WorkspaceSession,
)


class TestSessionEntity:
    def test_agent_session_binding_kind_is_literal(self):
        b = AgentSessionBinding(agent_id="ag-1")
        assert b.kind == "agent"

    def test_graph_session_binding_kind_is_literal(self):
        b = GraphSessionBinding(graph_id="gr-1")
        assert b.kind == "graph"

    def test_round_trip_with_agent_binding(self):
        s = WorkspaceSession(
            id="sess-1",
            workspace_id="ws-1",
            binding=AgentSessionBinding(agent_id="ag-1"),
            status=SessionStatus.CREATED,
            created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        again = WorkspaceSession.model_validate(s.model_dump(mode="json"))
        assert again.binding.kind == "agent"
        assert again.turn_no == 0
        assert again.cancel_requested is False
        assert again.pause_requested is False

    def test_round_trip_with_graph_binding(self):
        s = WorkspaceSession(
            id="sess-2",
            workspace_id="ws-1",
            binding=GraphSessionBinding(graph_id="gr-1"),
            status=SessionStatus.CREATED,
            created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        again = WorkspaceSession.model_validate(s.model_dump(mode="json"))
        assert again.binding.kind == "graph"

    def test_binding_discriminator_rejects_unknown_kind(self):
        with pytest.raises(ValidationError):
            WorkspaceSession.model_validate({
                "id": "sess-3",
                "workspace_id": "ws-1",
                "binding": {"kind": "mystery"},
                "status": "created",
                "created_at": "2026-05-10T00:00:00+00:00",
            })


class TestSessionMessageKind:
    """Verify SessionMessageKind enum variants (Task 3)."""

    def test_session_message_kind_variants(self):
        from primer.model.workspace_session import SessionMessageKind

        expected = {
            "user_input",
            "assistant_token",
            # Model reasoning text (S1 v2 transcript parity with chats).
            # Display only: skipped when rebuilding the prompt, because
            # replaying a model's own reasoning degrades the next turn.
            "reasoning",
            "tool_call",
            "tool_result",
            # Push-frame for an invoker-supplied external tool call, so a
            # live client sees it immediately and on reconnect replay.
            "external_tool_call",
            # Binding hand-off attribution: a session's agent can change
            # mid-workstream, and the shared transcript records who ran
            # which turn.
            "agent_marker",
            # Structural rewind marker: the replay walk drops visible
            # rows past its to_seq, keeping the log append-only.
            "rewind_marker",
            # Delivery frame for a notifying tool call (S3).
            "client_action",
            # One record per model call at the agent-loop seam (S7).
            "llm_call",
            "yielded",
            "resumed",
            "done",
            "cancelled",
            "error",
            # Graph-runtime node enter/exit transition (spec §2.6 /
            # plan Task 3.1). Shared 1:1 with TapEventClass.GRAPH_TRANSITION.
            "graph_transition",
            # Reset-same-session ENDED->CREATED marker (studio-agents-interact
            # §5.2 / plan Task 6).
            "invocation_divider",
            # Compaction summary marker: keeps messages.jsonl append-only so the
            # event log survives, while the compacted view is reconstructed at
            # read time. Skipped by the tap reader. Shared 1:1 with
            # TapEventClass.COMPACTION_MARKER.
            "compaction_marker",
        }
        actual = {k.value for k in SessionMessageKind}
        assert actual == expected


class TestSessionMessageRecord:
    """Verify SessionMessageRecord round-trip serialisation (Task 3)."""

    def test_session_message_record_round_trip(self):
        from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord

        rec = SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.ASSISTANT_TOKEN,
            payload={"delta": "hi"},
            created_at=datetime.now(timezone.utc),
        )
        j = rec.model_dump_json()
        parsed = SessionMessageRecord.model_validate_json(j)
        assert parsed == rec


class TestWorkspaceSessionStreamingFields:
    """Verify streaming lifecycle fields default correctly (Task 2)."""

    def test_workspace_session_streaming_fields_default(self):
        sess = WorkspaceSession(
            id="s1",
            workspace_id="w1",
            binding=AgentSessionBinding(agent_id="ag"),
            status=SessionStatus.CREATED,
            created_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
        )
        assert sess.turn_status == "idle"
        assert sess.cancel_requested_at is None
        assert sess.pause_requested_at is None
        assert sess.last_seq == 0


class TestSessionState:
    """session_state (01a04d91-a7a0, PHASE 1 of the execution-lifecycle
    revamp): one served truth derived from (status, parked_status,
    turn_status), computed fresh on every serialisation - never stored,
    never accepted as input."""

    def _sess(self, **overrides):
        base = dict(
            id="s1", workspace_id="w1",
            binding=AgentSessionBinding(agent_id="ag"),
            status=SessionStatus.RUNNING,
            created_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
        )
        base.update(overrides)
        return WorkspaceSession(**base)

    def test_ended_takes_precedence_over_everything(self):
        # Even a row that ALSO carries turn_status="running" (a stale/
        # transient combination) must read "ended" once status is ENDED -
        # terminal, nothing else matters once true.
        sess = self._sess(
            status=SessionStatus.ENDED, turn_status="running",
            parked_status="parked",
        )
        assert sess.session_state == "ended"

    def test_parked_takes_precedence_over_running(self):
        # The park write and the turn_status="idle" cleanup write are not
        # atomic with each other (dispatch.run_one_session_turn's
        # YieldToWorker branch vs. the streaming-phase finally moments
        # later) - a reader can observe turn_status still "running" for a
        # session that has already parked. Parked wins.
        sess = self._sess(turn_status="running", parked_status="parked")
        assert sess.session_state == "parked"

        sess2 = self._sess(turn_status="running", parked_status="resumable")
        assert sess2.session_state == "parked"

    def test_running_when_turn_status_running_and_not_parked(self):
        sess = self._sess(turn_status="running")
        assert sess.session_state == "running"

    def test_waiting_is_the_default_fallthrough(self):
        for status, turn_status in [
            (SessionStatus.CREATED, "idle"),
            (SessionStatus.RUNNING, "idle"),
            (SessionStatus.RUNNING, "claimable"),
            (SessionStatus.WAITING, "idle"),
            (SessionStatus.PAUSED, "idle"),
        ]:
            sess = self._sess(status=status, turn_status=turn_status)
            assert sess.session_state == "waiting", (status, turn_status)

    def test_computed_field_appears_in_serialisation(self):
        sess = self._sess(turn_status="running")
        dumped = sess.model_dump(mode="json")
        assert dumped["session_state"] == "running"

    def test_computed_field_is_not_accepted_as_input(self):
        # A round-trip through a previously-serialised dump (which now
        # includes session_state) must not choke on the extra key, and
        # must not let a client-supplied session_state override the
        # derived truth.
        sess = self._sess(turn_status="running")
        dumped = sess.model_dump(mode="json")
        dumped["session_state"] = "ended"  # a malicious/stale client value
        reloaded = WorkspaceSession.model_validate(dumped)
        assert reloaded.session_state == "running"

    def test_resting_after_a_completed_turn_reads_parked(self):
        # 01a0518a: _CLEAN_TURN_RESTS_PARKED leaves a clean stop at
        # WAITING (not ENDED) with turn_no already bumped past 0 - this
        # is the "parked" case the flip introduces. Covers all three
        # WAITING-producing reasons (clean rest, assistant-asked-a-
        # question, max_tokens/content_filter) since none of them leave
        # a distinguishing field behind - turn_no > 0 is the only signal
        # this vocabulary needs: "has this session ever produced a turn
        # to rest after". PAUSED is folded in too (an operator pause
        # after progress reads the same as a rest).
        for status in (SessionStatus.WAITING, SessionStatus.PAUSED):
            sess = self._sess(status=status, turn_status="idle", turn_no=1)
            assert sess.session_state == "parked", status

    def test_never_started_stays_waiting_even_at_waiting_status(self):
        # The distinguisher is turn_no, not status: a WAITING/PAUSED row
        # that has NEVER actually completed a turn (turn_no == 0 - not a
        # real production shape today, but the boundary the property
        # itself must get right) stays "waiting", not "parked".
        for status in (SessionStatus.WAITING, SessionStatus.PAUSED):
            sess = self._sess(status=status, turn_status="idle", turn_no=0)
            assert sess.session_state == "waiting", status

    def test_created_with_turn_no_zero_stays_waiting(self):
        # A CREATED session (never claimed) is turn_no == 0 by
        # definition - the "genuinely fresh" case the vocabulary reserves
        # "waiting" for.
        sess = self._sess(status=SessionStatus.CREATED, turn_status="idle", turn_no=0)
        assert sess.session_state == "waiting"

    def test_running_or_parked_status_still_win_over_turn_no(self):
        # The turn_no>0 "parked" branch is checked AFTER running/
        # parked_status - a session with turn_no>0 that is ACTIVELY
        # running or on a real yielding-tool park must not be
        # misclassified as merely "resting".
        running = self._sess(turn_status="running", turn_no=3)
        assert running.session_state == "running"

        yielded = self._sess(
            status=SessionStatus.WAITING, parked_status="parked", turn_no=3,
        )
        assert yielded.session_state == "parked"  # via parked_status, same value here
