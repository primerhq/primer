"""Tests for primer.agent.workspace_executor.WorkspaceAgentExecutor."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
)
from primer.model.except_ import ConflictError
from primer.model.provider import LLMModel
from primer.model.workspace_session import (
    AgentBinding,
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
)
from primer.session.persistence import WorkspaceMessageWriter
from primer.model.workspace import (
    FileMount,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.workspace import (
    LocalWorkspaceBackend,
    WorkspaceBackendFactory,
)


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeLLM:
    """Stub :class:`LLM` returning scripted streams turn-by-turn."""

    def __init__(self, *, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        idx = min(self._cursor, len(self._scripts) - 1)
        self._cursor += 1
        return self._stream_impl(self._scripts[idx])

    async def _stream_impl(
        self, events: list[StreamEvent]
    ) -> AsyncIterator[StreamEvent]:
        for ev in events:
            yield ev


# ===========================================================================
# Helpers
# ===========================================================================


def _agent(*, system_prompt=None) -> Agent:
    return Agent(
        id="researcher",
        description="Research agent",
        model=AgentModel(provider_id="openai-1", model_name="m"),
        system_prompt=list(system_prompt or []),
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="research",
        description="research workstation",
        provider_id="local-1",
        files=[
            FileMount(
                path="src/main.py",
                source={"kind": "inline", "content": "def main(): return 'TODO'\n"},
            ),
        ],
    )


async def _build_session(tmp_path: Path):
    config_entry = WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path=str(tmp_path / "wsroot")),
    )
    backend = WorkspaceBackendFactory.create(config_entry)
    assert isinstance(backend, LocalWorkspaceBackend)
    await backend.initialize()
    workspace = await backend.create(_template())
    binding = AgentBinding(
        agent_id="researcher",
        agent_name="Research Agent",
        registered_tool_ids=[],
    )
    session = await workspace.start_session(binding, instructions="hello")
    return backend, workspace, session


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


# ===========================================================================
# Construction & system prompt composition
# ===========================================================================


class TestConstruction:
    @pytest.mark.asyncio
    async def test_system_prompt_composed_with_workspace_fragment(
        self, tmp_path: Path
    ) -> None:
        backend, _, session = await _build_session(tmp_path)
        try:
            llm = _FakeLLM(
                scripts=[
                    [
                        TextDelta(text="ok", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ]
                ]
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={},
                session=session,
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(system_prompt=["base"]),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            await _drain(
                executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
            )
            sys_msg = llm.calls[0]["messages"][0]
            assert sys_msg.role == "system"
            sys_text = sys_msg.parts[0].text
            assert "base" in sys_text
            assert session.session_id in sys_text  # workspace fragment merged
        finally:
            await session.aclose()
            await backend.aclose()


# ===========================================================================
# Persistence via commit_state
# ===========================================================================


class TestPersistence:
    @pytest.mark.asyncio
    async def test_turn_persists_to_messages_jsonl(self, tmp_path: Path) -> None:
        backend, workspace, session = await _build_session(tmp_path)
        try:
            llm = _FakeLLM(
                scripts=[
                    [
                        TextDelta(text="hello!", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ]
                ]
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            await _drain(
                executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
            )
            jsonl_path = (
                workspace.root
                / workspace.template.state_path
                / "sessions"
                / session.session_id
                / "messages.jsonl"
            )
            content = jsonl_path.read_text(encoding="utf-8")
            lines = [ln for ln in content.splitlines() if ln.strip()]
            # Initial instruction "hello" + new user msg + assistant msg.
            assert len(lines) >= 3
            assert "hello!" in lines[-1]  # assistant text in last line
        finally:
            await session.aclose()
            await backend.aclose()


# ===========================================================================
# Status transitions
# ===========================================================================


class TestStatusTransitions:
    @pytest.mark.asyncio
    async def test_invoke_on_ended_session_raises(self, tmp_path: Path) -> None:
        backend, _, session = await _build_session(tmp_path)
        try:
            await session.aclose()  # ENDED
            llm = _FakeLLM(scripts=[[]])
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            with pytest.raises(ConflictError):
                await _drain(
                    executor.invoke(
                        [Message(role="user", parts=[TextPart(text="hi")])]
                    )
                )
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_question_mark_transitions_to_waiting(
        self, tmp_path: Path
    ) -> None:
        backend, _, session = await _build_session(tmp_path)
        try:
            llm = _FakeLLM(
                scripts=[
                    [
                        TextDelta(text="What's your scope?", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ]
                ]
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            await _drain(
                executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
            )
            assert await session.status() == SessionStatus.WAITING
            ws = await session.waiting_state()
            assert ws is not None
            assert ws.kind == "user_input"
            assert "scope" in ws.prompt
        finally:
            await session.aclose()
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_pause_request_transitions_to_paused(self, tmp_path: Path) -> None:
        backend, _, session = await _build_session(tmp_path)
        try:
            await session.request_pause()
            llm = _FakeLLM(scripts=[[]])
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )
            await _drain(
                executor.invoke([Message(role="user", parts=[TextPart(text="hi")])])
            )
            assert await session.status() == SessionStatus.PAUSED
            assert llm.calls == []
        finally:
            await session.aclose()
            await backend.aclose()


# ===========================================================================
# Sandbox (container/k8s) state-repo parity
# ===========================================================================


class _NoPathStateRepo:
    """Minimal sandbox-like StateRepo: serves history via ``read_state_file``
    only, with NO ``path`` attribute.

    This mirrors :class:`SandboxStateRepo`, whose state lives in the workspace
    pod and exposes no local filesystem ``path``. Reaching for ``._state.path``
    raised ``AttributeError`` and broke every agent session on a container/k8s
    backend on its first turn (FINDINGS F-K8S-AGENT). The executor must read
    history through the ``StateRepo.read_state_file`` protocol instead.
    """

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)

    async def read_state_file(self, path: str) -> bytes | None:
        return self._files.get(path)


class TestSandboxStateRepoParity:
    @pytest.mark.asyncio
    async def test_history_read_uses_read_state_file_not_path(
        self, tmp_path: Path
    ) -> None:
        """Loading history must not touch ``._state.path`` (absent on sandbox)."""
        backend, _, session = await _build_session(tmp_path)
        real_state = session._state
        try:
            prior = Message(role="user", parts=[TextPart(text="earlier turn")])
            rel = f"sessions/{session.session_id}/messages.jsonl"
            # Swap in a sandbox-like repo that has NO ``path`` attribute.
            session._state = _NoPathStateRepo(  # type: ignore[assignment]
                {rel: (prior.model_dump_json() + "\n").encode("utf-8")}
            )

            llm = _FakeLLM(
                scripts=[
                    [
                        TextDelta(text="reply", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ]
                ]
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )

            # Reading history must succeed (no AttributeError on ._state.path)
            # and surface the prior turn loaded via read_state_file.
            history = await executor._read_messages_jsonl()
            assert any(
                p.type == "text" and p.text == "earlier turn"
                for m in history
                for p in m.parts
            )

            text = await executor._read_messages_jsonl_text()
            assert "earlier turn" in text

            # An absent file returns empty rather than raising.
            session._state = _NoPathStateRepo({})  # type: ignore[assignment]
            assert await executor._read_messages_jsonl() == []
            assert await executor._read_messages_jsonl_text() == ""
        finally:
            # Restore the real repo so aclose()'s status commit works.
            session._state = real_state  # type: ignore[assignment]
            await session.aclose()
            await backend.aclose()


# ===========================================================================
# messages.jsonl rewrite races (arch-review batch 1, MEDIUM-1)
# ===========================================================================


class TestPersistTurnInstructionRace:
    """A steer must survive a concurrent ``_persist_turn``.

    ``_persist_turn`` is itself read-modify-rewrite: ``_appended_jsonl``
    snapshots messages.jsonl and ``commit_state`` rewrites the whole file
    from that snapshot. ``AgentSession.append_instruction`` does the same.
    Without serialisation an instruction committed inside the turn's
    read->rewrite gap is silently truncated by the turn's rewrite -- the
    user's steer is permanently lost. Both paths hold the session's
    ``messages_lock`` across their window so the steer survives.
    """

    @pytest.mark.asyncio
    async def test_instruction_survives_concurrent_persist_turn(
        self, tmp_path: Path
    ) -> None:
        backend, workspace, session = await _build_session(tmp_path)
        try:
            llm = _FakeLLM(
                scripts=[
                    [
                        TextDelta(text="assistant-turn-text", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ]
                ]
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_model(),
                tool_manager=mgr,
                session=session,
            )

            rel = f"sessions/{session.session_id}/messages.jsonl"
            real_read = workspace.state_repo.read_state_file
            real_persist = executor._persist_turn
            steer_started = asyncio.Event()
            steer_tasks: list[asyncio.Task] = []
            in_persist = False
            fired = False

            async def _do_steer() -> None:
                # Signal that the steer coroutine has begun, then append.
                # Under the fix this blocks on messages_lock until the turn's
                # rewrite releases it; without the fix it commits straight
                # into the read->rewrite gap and is then overwritten.
                steer_started.set()
                await session.append_instruction("steer me")

            async def hooked_read(path: str):
                nonlocal fired
                # Let _persist_turn take its snapshot FIRST, then hold the gap
                # open so the concurrent steer commits after that snapshot.
                # If the turn is not serialised, its rewrite drops the steer.
                result = await real_read(path)
                if in_persist and path == rel and not fired:
                    fired = True
                    steer_tasks.append(asyncio.create_task(_do_steer()))
                    await steer_started.wait()
                    # Give the (broken) unserialised steer time to commit, or
                    # the (fixed) serialised steer time to block on the lock.
                    await asyncio.sleep(0.05)
                return result

            async def hooked_persist(turn_messages) -> None:
                # Arm the read hook only for the reads _persist_turn itself
                # makes, so the interleave lands in exactly the window under
                # test (not _load_history's or _fetch_last_assistant_text's).
                nonlocal in_persist
                in_persist = True
                try:
                    await real_persist(turn_messages)
                finally:
                    in_persist = False

            workspace.state_repo.read_state_file = hooked_read  # type: ignore[assignment]
            executor._persist_turn = hooked_persist  # type: ignore[assignment]
            try:
                await _drain(
                    executor.invoke(
                        [Message(role="user", parts=[TextPart(text="hi")])]
                    )
                )
            finally:
                workspace.state_repo.read_state_file = real_read  # type: ignore[assignment]
            await asyncio.gather(*steer_tasks)

            content = (
                workspace.root
                / workspace.template.state_path
                / "sessions"
                / session.session_id
                / "messages.jsonl"
            ).read_bytes()
            # The steer committed inside the turn's read->rewrite window must
            # survive the rewrite.
            assert b"steer me" in content, content
            # ...and the turn the executor persisted must also be present.
            assert b"assistant-turn-text" in content, content
            # ...along with the original instruction that seeded the file.
            assert b"hello" in content, content
            # The interleave actually occurred (no vacuous pass).
            assert fired is True
        finally:
            await session.aclose()
            await backend.aclose()


# ===========================================================================
# Compaction preserves the event log + defers steers (PR-C)
# ===========================================================================


def _small_model() -> LLMModel:
    """Tiny context so a modest seeded history trips the compaction trigger."""
    return LLMModel(name="m", context_length=500)


def _messages_path(workspace, session):
    return (
        workspace.root
        / workspace.template.state_path
        / "sessions"
        / session.session_id
        / "messages.jsonl"
    )


def _event_log_records(text: str) -> list[dict]:
    """Parse the seq/kind SessionMessageRecord lines from messages.jsonl text."""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        import json as _json

        try:
            obj = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "kind" in obj and isinstance(obj.get("seq"), int):
            out.append(obj)
    return out


class TestCompactionPreservesEventLog:
    """Decision 4: compaction appends a marker; it never wipes the log."""

    @pytest.mark.asyncio
    async def test_event_log_survives_compaction(self, tmp_path: Path) -> None:
        backend, workspace, session = await _build_session(tmp_path)
        try:
            # Seed the append-only log with a MIX of event-log records
            # (seq 1..3, via the same writer the dispatch path uses) and the
            # initial "hello" Message line already written by start_session.
            writer = WorkspaceMessageWriter(
                workspace_io=workspace,
                session_id=session.session_id,
                start_seq=0,
            )
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            for kind, payload in (
                (SessionMessageKind.USER_INPUT, {"text": "seed-user"}),
                (SessionMessageKind.ASSISTANT_TOKEN, {"text": "seed-assistant"}),
                (SessionMessageKind.DONE, {"stop_reason": "stop"}),
            ):
                await writer.append(
                    SessionMessageRecord(
                        seq=1, kind=kind, payload=payload, created_at=now,
                    )
                )
            await writer.flush()

            # Seed enough Message-line history (big user turns) to trip the
            # compaction trigger for the tiny-context model below.
            for i in range(3):
                await session.append_instruction("PRE-COMPACTION-" + ("x" * 900))
                del i

            llm = _FakeLLM(
                scripts=[
                    # 1st stream call = the compaction summary.
                    [
                        TextDelta(text="COMPACTION-SUMMARY-TEXT", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ],
                    # 2nd stream call = the post-compaction turn.
                    [
                        TextDelta(text="post-compaction-turn", index=0),
                        Done(stop_reason="stop", raw_reason="stop"),
                    ],
                ]
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_small_model(),
                tool_manager=mgr,
                session=session,
                compaction=CompactionStrategy(tail_turns=1),
            )

            await _drain(executor.invoke([]))

            text = _messages_path(workspace, session).read_text(encoding="utf-8")
            records = _event_log_records(text)
            by_kind = {r["kind"]: r for r in records}

            # (i) the pre-existing event-log records survive with original seqs.
            assert by_kind["user_input"]["seq"] == 1
            assert by_kind["assistant_token"]["seq"] == 2
            assert by_kind["done"]["seq"] == 3

            # (ii) exactly one compaction_marker, with a STRICTLY greater seq.
            markers = [r for r in records if r["kind"] == "compaction_marker"]
            assert len(markers) == 1, markers
            assert markers[0]["seq"] > 3
            # The stored summary is the strategy's full summary message text
            # (it carries an "[earlier conversation compacted ...]" preamble).
            assert "COMPACTION-SUMMARY-TEXT" in markers[0]["payload"]["summary"]
            assert markers[0]["payload"]["replaced_to_seq"] == 3

            # (iii) the reader returns the COMPACTED view (summary + tail), not
            # the raw pre-compaction messages.
            history = await executor._read_messages_jsonl()
            assert history[0].role == "assistant"
            assert "COMPACTION-SUMMARY-TEXT" in history[0].parts[0].text
            joined = "\n".join(
                p.text
                for m in history
                for p in m.parts
                if getattr(p, "type", None) == "text"
            )
            assert "PRE-COMPACTION-" not in joined  # folded into the summary
        finally:
            await session.aclose()
            await backend.aclose()


class _BlockingCompactionLLM:
    """First stream call (compaction) parks on ``release`` after signalling
    ``entered``; the second call (the turn) streams immediately."""

    def __init__(self, *, summary_text, turn_text, entered, release) -> None:
        self._summary_text = summary_text
        self._turn_text = turn_text
        self._entered = entered
        self._release = release
        self._calls = 0

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs) -> AsyncIterator[StreamEvent]:
        self._calls += 1
        if self._calls == 1:
            return self._compaction_stream()
        return self._turn_stream()

    async def _compaction_stream(self) -> AsyncIterator[StreamEvent]:
        self._entered.set()
        await self._release.wait()
        yield TextDelta(text=self._summary_text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def _turn_stream(self) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text=self._turn_text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


async def _seed_compactable_history(session) -> None:
    for _ in range(3):
        await session.append_instruction("PRE-COMPACTION-" + ("x" * 900))


class TestSteerDeferredDuringCompaction:
    """Decision 5: a steer during compaction is deferred, never lost, and
    applied AFTER compaction in submission order."""

    @pytest.mark.asyncio
    async def test_steer_during_compaction_is_deferred_not_lost_and_applied_after(
        self, tmp_path: Path
    ) -> None:
        backend, workspace, session = await _build_session(tmp_path)
        try:
            await _seed_compactable_history(session)
            entered = asyncio.Event()
            release = asyncio.Event()
            llm = _BlockingCompactionLLM(
                summary_text="COMPACTION-SUMMARY",
                turn_text="post-compaction-turn",
                entered=entered,
                release=release,
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_small_model(),
                tool_manager=mgr,
                session=session,
                compaction=CompactionStrategy(tail_turns=1),
            )

            invoke_task = asyncio.create_task(_drain(executor.invoke([])))
            # Wait until the compaction LLM is parked mid-summarise.
            await asyncio.wait_for(entered.wait(), timeout=5.0)

            # The window is open: the flag is set...
            assert session._state.is_compacting(session.session_id) is True

            # ...so a steer arriving now is DEFERRED, not committed.
            await session.append_instruction("STEER-DURING-COMPACTION")

            during = _messages_path(workspace, session).read_text(encoding="utf-8")
            assert "STEER-DURING-COMPACTION" not in during  # not yet on disk
            pending = session._state._pending_steers.get(session.session_id, [])
            assert any(
                "STEER-DURING-COMPACTION" in p.parts[0].text for p in pending
            )  # recorded PENDING

            # Release the compaction LLM; let the turn finish.
            release.set()
            await asyncio.wait_for(invoke_task, timeout=5.0)

            text = _messages_path(workspace, session).read_text(encoding="utf-8")
            lines = text.splitlines()
            marker_idx = next(
                i for i, ln in enumerate(lines) if '"compaction_marker"' in ln
            )
            steer_idx = next(
                i for i, ln in enumerate(lines) if "STEER-DURING-COMPACTION" in ln
            )
            # The summary is present AND the steer is present AND the steer was
            # applied AFTER the marker (applied-after ordering).
            assert "COMPACTION-SUMMARY" in text
            assert steer_idx > marker_idx

            # Non-vacuous: the interleave actually fired.
            assert entered.is_set() is True
            # And the flag has been cleared again.
            assert session._state.is_compacting(session.session_id) is False
        finally:
            await session.aclose()
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_multiple_steers_during_compaction_survive_in_order(
        self, tmp_path: Path
    ) -> None:
        backend, workspace, session = await _build_session(tmp_path)
        try:
            await _seed_compactable_history(session)
            entered = asyncio.Event()
            release = asyncio.Event()
            llm = _BlockingCompactionLLM(
                summary_text="COMPACTION-SUMMARY",
                turn_text="post-compaction-turn",
                entered=entered,
                release=release,
            )
            mgr = ToolExecutionManager.for_workspace(
                toolset_providers={}, session=session
            )
            executor = WorkspaceAgentExecutor(
                agent=_agent(),
                llm=llm,  # type: ignore[arg-type]
                llm_model=_small_model(),
                tool_manager=mgr,
                session=session,
                compaction=CompactionStrategy(tail_turns=1),
            )

            invoke_task = asyncio.create_task(_drain(executor.invoke([])))
            await asyncio.wait_for(entered.wait(), timeout=5.0)

            # Two steers during ONE compaction window, submitted in order.
            await session.append_instruction("STEER-ONE")
            await session.append_instruction("STEER-TWO")

            release.set()
            await asyncio.wait_for(invoke_task, timeout=5.0)

            text = _messages_path(workspace, session).read_text(encoding="utf-8")
            lines = text.splitlines()
            marker_idx = next(
                i for i, ln in enumerate(lines) if '"compaction_marker"' in ln
            )
            one_idx = next(i for i, ln in enumerate(lines) if "STEER-ONE" in ln)
            two_idx = next(i for i, ln in enumerate(lines) if "STEER-TWO" in ln)
            # Both survived, both after the marker, in submission order.
            assert marker_idx < one_idx < two_idx
        finally:
            await session.aclose()
            await backend.aclose()
