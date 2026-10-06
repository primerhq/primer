"""run_one_session_turn must forward a parked-on-user-input session to the
channel dispatcher, so ask_user / tool_approval prompts reach Slack /
Telegram / Discord.

Regression: the dispatch existed as ``_dispatch_to_channels`` but had no
production caller -- the park branch returned the outcome without ever
invoking it, so channels never received any prompt.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.adapter import PromptEnvelope
from primer.int.claim import ClaimKind, Lease
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_lease(session_id: str = "s1") -> Lease:
    now = _now()
    return Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
        claimed_at=now, expires_at=now, attempt_count=1, last_error=None,
    )


class FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = defaultdict(bytes)

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self._data[(session_id, "messages.jsonl")] += line


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[PromptEnvelope] = []

    async def dispatch_prompt(self, *, envelope: PromptEnvelope, session=None) -> list:
        self.calls.append(envelope)
        return [{"ok": True}]


async def _seed_session(storage_provider, session_id: str = "s1") -> WorkspaceSession:
    sess = WorkspaceSession(
        id=session_id, workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=_now(), turn_status="running",
    )
    await storage_provider.get_storage(WorkspaceSession).create(sess)
    return sess


@pytest.fixture
def fake_storage_provider():
    from tests.conftest import _FakeStorageProvider
    return _FakeStorageProvider()


@pytest.fixture
async def fake_event_bus():
    bus = InMemoryEventBus()
    await bus.initialize()
    yield bus
    await bus.aclose()


def _yielding_deps(storage, bus, dispatcher, exc):
    class _YieldingExecutor:
        async def invoke(self, messages: list[Any], **kwargs: Any):
            raise exc
            yield

    async def _build_executor(session: WorkspaceSession):
        return _YieldingExecutor()

    return SessionDispatchDeps(
        storage_provider=storage,
        workspace_io=FakeWorkspaceIO(),
        event_bus=bus,
        build_executor=_build_executor,
        channel_dispatcher=dispatcher,
    )


@pytest.mark.asyncio
async def test_ask_user_park_dispatches_to_channels(
    fake_storage_provider, fake_event_bus,
) -> None:
    sess = await _seed_session(fake_storage_provider)
    yielded = Yielded(
        tool_name="ask_user", event_key="ask_user:s1:tc-1",
        resume_metadata={"prompt": "what is your name?"},
    )
    exc = YieldToWorker(yielded, tool_call_id="tc-1", llm_messages=[])
    dispatcher = _RecordingDispatcher()
    deps = _yielding_deps(fake_storage_provider, fake_event_bus, dispatcher, exc)

    outcome = await run_one_session_turn(_make_lease(sess.id), deps)

    assert outcome.park is not None
    assert len(dispatcher.calls) == 1
    env = dispatcher.calls[0]
    assert env.kind == "ask_user"
    assert env.workspace_id == "w1"
    assert env.session_id == "s1"
    assert env.tool_call_id == "tc-1"
    assert env.prompt == "what is your name?"


@pytest.mark.asyncio
async def test_approval_park_dispatches_with_choices(
    fake_storage_provider, fake_event_bus,
) -> None:
    sess = await _seed_session(fake_storage_provider)
    yielded = Yielded(
        tool_name="_approval", event_key="tool_approval:s1:tc-2",
        resume_metadata={
            "gate_reason": "always",
            "original_call": {"id": "tc-2", "name": "delete_workspace",
                              "arguments": {"id": "ws-x"}},
        },
    )
    exc = YieldToWorker(yielded, tool_call_id="tc-2", llm_messages=[])
    dispatcher = _RecordingDispatcher()
    deps = _yielding_deps(fake_storage_provider, fake_event_bus, dispatcher, exc)

    await run_one_session_turn(_make_lease(sess.id), deps)

    assert len(dispatcher.calls) == 1
    env = dispatcher.calls[0]
    assert env.kind == "tool_approval"
    assert env.choices == ["Approve", "Reject"]
    assert "delete_workspace" in env.prompt


@pytest.mark.asyncio
async def test_no_dispatcher_park_is_still_ok(
    fake_storage_provider, fake_event_bus,
) -> None:
    """Park must succeed even when no channel dispatcher is wired."""
    sess = await _seed_session(fake_storage_provider)
    yielded = Yielded(
        tool_name="ask_user", event_key="ask_user:s1:tc-3",
        resume_metadata={"prompt": "hi?"},
    )
    exc = YieldToWorker(yielded, tool_call_id="tc-3", llm_messages=[])
    deps = _yielding_deps(fake_storage_provider, fake_event_bus, None, exc)

    outcome = await run_one_session_turn(_make_lease(sess.id), deps)
    assert outcome.park is not None


# ---------------------------------------------------------------------------
# the fan-out is best-effort: a failure of it never fails or aborts a park that was already decided
# ---------------------------------------------------------------------------


async def _park_with_dispatcher(storage, bus, dispatcher, yielded, *, deps_extra=None):
    sess = await _seed_session(storage)
    exc = YieldToWorker(yielded, tool_call_id="tc-bf", llm_messages=[])
    deps = _yielding_deps(storage, bus, dispatcher, exc)
    for name, value in (deps_extra or {}).items():
        setattr(deps, name, value)
    return await run_one_session_turn(_make_lease(sess.id), deps)


@pytest.mark.asyncio
async def test_a_prompt_envelope_that_cannot_be_built_does_not_fail_the_park(
    fake_storage_provider, fake_event_bus, monkeypatch, caplog,
) -> None:
    """``_build_prompt_envelope`` ran OUTSIDE the dispatcher's ``try``: an error in it (a malformed ``resume_metadata``)
    propagated out of ``run_one_session_turn`` and the turn that had decided to park raised instead, with the park
    never released. It is logged at ERROR with the session id, nothing is sent, and the park lands."""
    import logging

    import primer.worker.yield_runtime as yield_runtime

    def boom(**kwargs):
        raise ValueError("resume_metadata is not what the envelope builder expects")

    monkeypatch.setattr(yield_runtime, "_build_prompt_envelope", boom)
    dispatcher = _RecordingDispatcher()
    yielded = Yielded(tool_name="ask_user", event_key="ask_user:s1:tc-bf", resume_metadata={"prompt": "name?"})

    with caplog.at_level(logging.ERROR):
        outcome = await _park_with_dispatcher(fake_storage_provider, fake_event_bus, dispatcher, yielded)

    assert outcome.park is not None and outcome.success is True, "the park did not land"
    assert dispatcher.calls == [], "a prompt was sent although its envelope could not be built"
    assert any(
        r.levelno >= logging.ERROR and "s1" in r.getMessage() and "envelope" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_an_ask_user_files_read_that_fails_still_sends_the_prompt_without_media(
    fake_storage_provider, fake_event_bus, monkeypatch, caplog,
) -> None:
    """Reading an ask_user's ``files`` into media was unguarded past the registry lookups: a failure of it escaped the
    park. The prompt is worth more than its attachment, so it is sent WITHOUT media, with an ERROR naming the session."""
    import logging
    from types import SimpleNamespace

    import primer.channel.media as media

    async def failing_read(workspace, store, files):
        raise OSError("the workspace volume is not answering")

    monkeypatch.setattr(media, "media_from_workspace_files", failing_read)

    async def get_workspace(workspace_id):
        return object()

    async def get_default():
        return object()

    dispatcher = _RecordingDispatcher()
    yielded = Yielded(
        tool_name="ask_user", event_key="ask_user:s1:tc-bf",
        resume_metadata={"prompt": "which file?", "files": ["a.txt"]},
    )

    with caplog.at_level(logging.ERROR):
        outcome = await _park_with_dispatcher(
            fake_storage_provider, fake_event_bus, dispatcher, yielded,
            deps_extra={
                "workspace_registry": SimpleNamespace(get_workspace=get_workspace),
                "artifact_registry": SimpleNamespace(get_default=get_default),
            },
        )

    assert outcome.park is not None, "the park did not land"
    assert len(dispatcher.calls) == 1, "the prompt was not sent"
    assert not dispatcher.calls[0].media, "the envelope carries media although the read failed"
    assert any(r.levelno >= logging.ERROR and "s1" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_one_pending_node_whose_envelope_cannot_be_built_does_not_drop_the_others(monkeypatch, caplog) -> None:
    """The multi-event fan-out of a graph park: an error building ONE node's envelope is logged and skipped; the other
    nodes are still prompted, none is reported sent for the bad one (so a later re-park may try it again), and nothing
    propagates."""
    import logging

    import primer.worker.yield_runtime as yield_runtime
    from primer.worker.yield_runtime import _dispatch_to_channels_multi

    real = yield_runtime._build_prompt_envelope

    def flaky(**kwargs):
        if kwargs["fallback_tool_call_id"] == "tc-bad":
            raise ValueError("malformed metadata")
        return real(**kwargs)

    monkeypatch.setattr(yield_runtime, "_build_prompt_envelope", flaky)
    dispatcher = _RecordingDispatcher()
    pending = [
        {"kind": "ask_user", "node_id": "n1", "tool_call_id": "tc-bad", "resume_metadata": {"prompt": "a?"}},
        {"kind": "ask_user", "node_id": "n2", "tool_call_id": "tc-good", "resume_metadata": {"prompt": "b?"}},
    ]

    with caplog.at_level(logging.ERROR):
        sent = await _dispatch_to_channels_multi(
            dispatcher=dispatcher, workspace_id="w1", session_id="s1", pending=pending, already_sent=set(),
        )

    assert sent == {("n2", "tc-good")}, f"sent: {sent}"
    assert [c.tool_call_id for c in dispatcher.calls] == ["tc-good"]
    assert any(r.levelno >= logging.ERROR and "s1" in r.getMessage() and "tc-bad" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_cancellation_during_the_fan_out_still_propagates(monkeypatch) -> None:
    """Best effort is ``except Exception``: a cancelled turn must still cancel, in the single-node and the graph fan-out."""
    import asyncio
    from types import SimpleNamespace

    import primer.worker.yield_runtime as yield_runtime

    def cancelled(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(yield_runtime, "_build_prompt_envelope", cancelled)
    yielded = Yielded(tool_name="ask_user", event_key="ask_user:s1:tc-c", resume_metadata={"prompt": "x?"})
    with pytest.raises(asyncio.CancelledError):
        await yield_runtime._dispatch_to_channels(
            dispatcher=_RecordingDispatcher(), session=SimpleNamespace(id="s1", workspace_id="w1"), yielded=yielded,
        )
    pending = [{"kind": "ask_user", "node_id": "n1", "tool_call_id": "tc-c", "resume_metadata": {"prompt": "x?"}}]
    with pytest.raises(asyncio.CancelledError):
        await yield_runtime._dispatch_to_channels_multi(
            dispatcher=_RecordingDispatcher(), workspace_id="w1", session_id="s1", pending=pending, already_sent=set(),
        )


@pytest.mark.asyncio
async def test_a_multi_node_graph_park_with_one_unbuildable_envelope_still_lands_and_prompts_the_others(
    monkeypatch, caplog,
) -> None:
    """Through the REAL park arm (``run_one_session_turn``), not a direct call: a graph park with two pending gates where
    ONE gate's prompt envelope cannot be built. The park lands, the other gate is still prompted on the channels, and an
    ERROR naming the session and the node is logged (the per-node guard of ``_dispatch_to_channels_multi``)."""
    import logging

    import primer.worker.yield_runtime as yield_runtime
    from tests.session.test_tool_wait_park_invariant_fails_turn import _RecordingDispatcher as _Recorder
    from tests.session.test_tool_wait_park_invariant_fails_turn import _setup

    _storage, session_id, _io, _lines, deps, lease = await _setup("yield_mixed_multi")
    deps.channel_dispatcher = _Recorder()
    real = yield_runtime._build_prompt_envelope

    def flaky(**kwargs):
        if kwargs["fallback_tool_call_id"] == "gate-a":
            raise ValueError("the first gate's metadata is malformed")
        return real(**kwargs)

    monkeypatch.setattr(yield_runtime, "_build_prompt_envelope", flaky)

    with caplog.at_level(logging.ERROR):
        outcome = await run_one_session_turn(lease, deps)

    assert outcome.success is True and outcome.park is not None, "the park did not land"
    assert [(e.tool_call_id, e.prompt) for e in deps.channel_dispatcher.prompts] == [("gate-b", "size?")]
    assert any(
        r.levelno >= logging.ERROR and session_id in r.getMessage() and "gate-a" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_a_dispatcher_that_fails_for_one_node_does_not_drop_the_others_prompts(caplog) -> None:
    """The multi fan-out's OTHER guard, around ``dispatcher.dispatch_prompt`` itself: one node's delivery failure is
    logged with the session id, the other nodes are still prompted, and only the delivered ones are reported sent
    (so a later re-park retries the failed one)."""
    import logging

    from primer.worker.yield_runtime import _dispatch_to_channels_multi

    class _FlakyDispatcher:
        def __init__(self) -> None:
            self.delivered: list[str] = []

        async def dispatch_prompt(self, *, envelope, session=None):
            if envelope.tool_call_id == "tc-down":
                raise ConnectionError("the channel is unreachable")
            self.delivered.append(envelope.tool_call_id)
            return [{"ok": True}]

    dispatcher = _FlakyDispatcher()
    pending = [
        {"kind": "ask_user", "node_id": "n1", "tool_call_id": "tc-down", "resume_metadata": {"prompt": "a?"}},
        {"kind": "ask_user", "node_id": "n2", "tool_call_id": "tc-up", "resume_metadata": {"prompt": "b?"}},
    ]

    with caplog.at_level(logging.ERROR):
        sent = await _dispatch_to_channels_multi(
            dispatcher=dispatcher, workspace_id="w1", session_id="s1", pending=pending, already_sent=set(),
        )

    assert dispatcher.delivered == ["tc-up"]
    assert sent == {("n2", "tc-up")}, f"sent: {sent}"
    assert any(r.levelno >= logging.ERROR and "s1" in r.getMessage() and "tc-down" in r.getMessage() for r in caplog.records)
