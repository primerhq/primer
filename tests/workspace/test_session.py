"""Tests for matrix.workspace.session.AgentSession."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from matrix.model.chat import Message, TextPart
from matrix.model.except_ import ConflictError
from matrix.model.workspace_session import (
    AgentBinding,
    SessionStatus,
)
from matrix.workspace.local.cache import LocalTruncationStore as TruncationStore
from matrix.workspace.local.state import LocalStateRepo as StateRepo
from matrix.workspace.session import AgentSession


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH",
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
async def state_repo(tmp_path: Path) -> StateRepo:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
    await repo.initialize()
    return repo


@pytest.fixture
def truncation_store(tmp_path: Path) -> TruncationStore:
    return TruncationStore(tmp_path / ".tmp")


def _make_binding(*, agent_id: str = "agent-foo") -> AgentBinding:
    return AgentBinding(agent_id=agent_id, agent_name=f"Agent {agent_id}")


async def _start_session(
    state_repo: StateRepo,
    truncation_store: TruncationStore,
    *,
    session_id: str = "sess-1",
    agent_id: str = "agent-foo",
    instructions: str | None = None,
) -> AgentSession:
    return await AgentSession.start(
        session_id=session_id,
        workspace_id="ws-1",
        agent_binding=_make_binding(agent_id=agent_id),
        state_repo=state_repo,
        truncation_store=truncation_store,
        instructions=instructions,
    )


# ===========================================================================
# start() / construction
# ===========================================================================


class TestStart:
    async def test_creates_session_slot(
        self, state_repo: StateRepo, truncation_store: TruncationStore, tmp_path: Path
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        slot = tmp_path / ".state" / "sessions" / "sess-1"
        assert (slot / "session.json").exists()
        assert (slot / "agent.json").exists()
        assert session.session_id == "sess-1"
        assert session.agent_id == "agent-foo"
        assert session.workspace_id == "ws-1"

    async def test_starts_in_running_status(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        assert await session.status() == SessionStatus.RUNNING

    async def test_initial_instructions_appended(
        self, state_repo: StateRepo, truncation_store: TruncationStore, tmp_path: Path
    ) -> None:
        session = await _start_session(
            state_repo, truncation_store, instructions="hello there"
        )
        msgs_path = tmp_path / ".state" / "sessions" / "sess-1" / "messages.jsonl"
        assert msgs_path.exists()
        # Verify the initial instruction appears in messages.jsonl as a user-role message.
        pending = await session.take_pending_messages()
        assert len(pending) == 1
        assert pending[0].role == "user"
        assert any(
            isinstance(p, TextPart) and p.text == "hello there"
            for p in pending[0].parts
        )

    async def test_no_instructions_no_messages_file(
        self, state_repo: StateRepo, truncation_store: TruncationStore, tmp_path: Path
    ) -> None:
        await _start_session(state_repo, truncation_store)
        msgs_path = tmp_path / ".state" / "sessions" / "sess-1" / "messages.jsonl"
        assert not msgs_path.exists()

    async def test_constructor_rejects_mismatched_agent_id(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        from datetime import datetime, timezone

        from matrix.model.workspace_session import SessionInfo

        info = SessionInfo(
            session_id="sess-x",
            agent_id="agent-mismatch",
            workspace_id="ws-1",
            status=SessionStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            last_activity_at=datetime.now(timezone.utc),
        )
        binding = _make_binding(agent_id="agent-other")
        with pytest.raises(ValueError, match="agent_id"):
            AgentSession(
                session_info=info,
                agent_binding=binding,
                state_repo=state_repo,
                truncation_store=truncation_store,
            )

    async def test_workspace_tools_default_empty(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        assert session.workspace_tools == []

    async def test_system_prompt_fragment_includes_session_id(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        assert "sess-1" in session.system_prompt_fragment


# ===========================================================================
# append_instruction()
# ===========================================================================


class TestAppendInstruction:
    async def test_writes_user_message(
        self, state_repo: StateRepo, truncation_store: TruncationStore, tmp_path: Path
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        instr = await session.append_instruction("do the thing")
        assert instr.session_id == "sess-1"
        assert instr.content == "do the thing"
        msgs_path = tmp_path / ".state" / "sessions" / "sess-1" / "messages.jsonl"
        content = msgs_path.read_text(encoding="utf-8")
        assert "do the thing" in content

    async def test_returns_unique_instruction_ids(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        i1 = await session.append_instruction("a")
        i2 = await session.append_instruction("b")
        assert i1.instruction_id != i2.instruction_id

    async def test_appends_rather_than_overwrites(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(
            state_repo, truncation_store, instructions="first"
        )
        await session.append_instruction("second")
        pending = await session.take_pending_messages()
        # Both messages still present (no assistant turn has consumed them).
        assert len(pending) == 2

    async def test_rejects_empty_content(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        with pytest.raises(ValueError, match="non-empty"):
            await session.append_instruction("")

    async def test_rejects_when_ended(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.aclose()
        with pytest.raises(ConflictError, match="ENDED"):
            await session.append_instruction("too late")

    async def test_allowed_in_waiting(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        from datetime import datetime, timezone

        from matrix.model.workspace_session import _UserInputWaiting  # type: ignore[attr-defined]

        session = await _start_session(state_repo, truncation_store)
        await session.set_status(
            SessionStatus.WAITING,
            waiting_state=_UserInputWaiting(
                prompt="?",
                queued_at=datetime.now(timezone.utc),
            ),
        )
        # User responding to the waiting state.
        await session.append_instruction("here is the answer")

    async def test_commit_uses_user_instruction_op(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.append_instruction("hi")
        ops = [c.op for c in await state_repo.history()]
        assert "user_instruction" in ops


# ===========================================================================
# take_pending_messages()
# ===========================================================================


class TestTakePendingMessages:
    async def test_empty_for_fresh_session(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        assert await session.take_pending_messages() == []

    async def test_returns_messages_after_last_assistant_turn(
        self, state_repo: StateRepo, truncation_store: TruncationStore, tmp_path: Path
    ) -> None:
        session = await _start_session(
            state_repo, truncation_store, instructions="first"
        )
        # Manually append an assistant message via commit_state.
        msgs_path = tmp_path / ".state" / "sessions" / "sess-1" / "messages.jsonl"
        existing = msgs_path.read_text(encoding="utf-8")
        assistant_msg = Message(role="assistant", parts=[TextPart(text="reply 1")])
        new_content = existing + assistant_msg.model_dump_json() + "\n"
        await session.commit_state(
            summary="assistant turn",
            op="message",
            files={"messages.jsonl": new_content},
        )
        # After the assistant turn, take_pending_messages returns nothing.
        assert await session.take_pending_messages() == []
        # New user instruction lands AFTER the assistant turn.
        await session.append_instruction("follow-up")
        pending = await session.take_pending_messages()
        assert len(pending) == 1
        assert pending[0].role == "user"

    async def test_returns_all_when_no_assistant_yet(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.append_instruction("a")
        await session.append_instruction("b")
        pending = await session.take_pending_messages()
        assert len(pending) == 2
        assert all(m.role == "user" for m in pending)

    async def test_idempotent_within_a_turn(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        # The "cursor" only advances when a new assistant message lands;
        # repeated calls between assistant messages return the same set.
        session = await _start_session(
            state_repo, truncation_store, instructions="first"
        )
        a = await session.take_pending_messages()
        b = await session.take_pending_messages()
        assert len(a) == 1
        assert len(b) == 1


# ===========================================================================
# Pause / resume / end flag semantics
# ===========================================================================


class TestPauseResumeEndFlags:
    async def test_initial_flags_are_clear(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        assert session.pause_requested is False
        assert session.end_requested is False

    async def test_request_pause_sets_flag(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.request_pause(reason="thinking")
        assert session.pause_requested is True

    async def test_request_resume_clears_flag(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.request_pause()
        await session.request_resume()
        assert session.pause_requested is False

    async def test_request_pause_idempotent(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.request_pause()
        await session.request_pause()  # no-op
        assert session.pause_requested is True

    async def test_request_pause_rejected_when_ended(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.aclose()
        with pytest.raises(ConflictError, match="ENDED"):
            await session.request_pause()

    async def test_request_resume_rejected_when_ended(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.aclose()
        with pytest.raises(ConflictError, match="ENDED"):
            await session.request_resume()

    async def test_request_end_sets_flag(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.request_end()
        assert session.end_requested is True

    async def test_request_end_idempotent_on_already_ended(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.aclose()
        # request_end on ENDED session sets the flag without raising.
        await session.request_end()
        assert session.end_requested is True


# ===========================================================================
# set_status() — transitions
# ===========================================================================


class TestSetStatus:
    async def test_running_to_paused(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.set_status(SessionStatus.PAUSED)
        assert await session.status() == SessionStatus.PAUSED

    async def test_running_to_waiting_writes_waiting_json(
        self,
        state_repo: StateRepo,
        truncation_store: TruncationStore,
        tmp_path: Path,
    ) -> None:
        from datetime import datetime, timezone

        from matrix.model.workspace_session import _UserInputWaiting  # type: ignore[attr-defined]

        session = await _start_session(state_repo, truncation_store)
        await session.set_status(
            SessionStatus.WAITING,
            waiting_state=_UserInputWaiting(
                prompt="What now?",
                queued_at=datetime.now(timezone.utc),
            ),
        )
        waiting_path = tmp_path / ".state" / "sessions" / "sess-1" / "waiting.json"
        assert waiting_path.exists()
        ws = await session.waiting_state()
        assert ws is not None and ws.kind == "user_input"

    async def test_waiting_to_running_deletes_waiting_json(
        self,
        state_repo: StateRepo,
        truncation_store: TruncationStore,
        tmp_path: Path,
    ) -> None:
        from datetime import datetime, timezone

        from matrix.model.workspace_session import _UserInputWaiting  # type: ignore[attr-defined]

        session = await _start_session(state_repo, truncation_store)
        await session.set_status(
            SessionStatus.WAITING,
            waiting_state=_UserInputWaiting(
                prompt="?",
                queued_at=datetime.now(timezone.utc),
            ),
        )
        waiting_path = tmp_path / ".state" / "sessions" / "sess-1" / "waiting.json"
        assert waiting_path.exists()
        await session.set_status(SessionStatus.RUNNING)
        assert not waiting_path.exists()

    async def test_waiting_state_returns_none_when_not_waiting(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        assert await session.waiting_state() is None

    async def test_waiting_requires_waiting_state(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        with pytest.raises(ConflictError, match="waiting_state"):
            await session.set_status(SessionStatus.WAITING)

    async def test_ended_requires_ended_reason(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        with pytest.raises(ConflictError, match="ended_reason"):
            await session.set_status(SessionStatus.ENDED)

    async def test_ended_is_terminal(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.set_status(SessionStatus.ENDED, ended_reason="completed")
        with pytest.raises(ConflictError, match="illegal transition"):
            await session.set_status(SessionStatus.RUNNING)

    async def test_ended_records_ended_at_and_reason(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.set_status(SessionStatus.ENDED, ended_reason="failed")
        info = await session.info()
        assert info.status == SessionStatus.ENDED
        assert info.ended_reason == "failed"
        assert info.ended_at is not None

    async def test_ended_reaps_tmp_subdirectory(
        self,
        state_repo: StateRepo,
        truncation_store: TruncationStore,
        tmp_path: Path,
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.cache_output("payload")
        sess_tmp = tmp_path / ".tmp" / "sess-1"
        assert sess_tmp.is_dir()
        await session.set_status(SessionStatus.ENDED, ended_reason="cancelled")
        assert not sess_tmp.exists()

    async def test_status_change_carries_op(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.set_status(SessionStatus.PAUSED)
        latest = (await state_repo.history(limit=1))[0]
        assert latest.op == "status_change"


# ===========================================================================
# commit_state()
# ===========================================================================


class TestCommitState:
    async def test_commits_files(
        self,
        state_repo: StateRepo,
        truncation_store: TruncationStore,
        tmp_path: Path,
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        sha = await session.commit_state(
            summary="todo update",
            op="todo_update",
            files={"todos.json": '{"items":[]}'},
        )
        assert len(sha) == 40
        slot = tmp_path / ".state" / "sessions" / "sess-1"
        assert (slot / "todos.json").exists()

    async def test_rejects_when_ended(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.aclose()
        with pytest.raises(ConflictError, match="ENDED"):
            await session.commit_state(summary="x", op="message")

    async def test_propagates_tool_and_call_id(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.commit_state(
            summary="tool call",
            op="tool_call",
            tool="exec",
            call_id="c-1",
        )
        latest = (await state_repo.history(limit=1))[0]
        assert latest.tool == "exec"
        assert latest.call_id == "c-1"


# ===========================================================================
# cache_output()
# ===========================================================================


class TestCacheOutput:
    async def test_writes_to_session_subdirectory(
        self,
        state_repo: StateRepo,
        truncation_store: TruncationStore,
        tmp_path: Path,
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        path = await session.cache_output("hello")
        from pathlib import Path as _P

        assert _P(path).parent == tmp_path / ".tmp" / "sess-1"
        assert _P(path).read_text(encoding="utf-8") == "hello"


# ===========================================================================
# aclose()
# ===========================================================================


class TestAclose:
    async def test_transitions_to_ended_completed(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.aclose()
        info = await session.info()
        assert info.status == SessionStatus.ENDED
        assert info.ended_reason == "completed"

    async def test_idempotent(
        self, state_repo: StateRepo, truncation_store: TruncationStore
    ) -> None:
        session = await _start_session(state_repo, truncation_store)
        await session.aclose()
        await session.aclose()  # no-op the second time
        assert await session.status() == SessionStatus.ENDED
