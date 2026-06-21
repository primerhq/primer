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
