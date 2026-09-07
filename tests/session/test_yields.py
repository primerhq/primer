"""Unit tests for :mod:`primer.session.yields`.

Confirms the extracted service helper reaches the same wake path the
REST yield-respond endpoint uses (publish onto the parked event_key),
including the validation parity the dispatcher relies on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.yields import (
    RespondToYieldDeps,
    respond_to_yield,
    tool_wait_event_key,
)


class _FakeEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


def _parked_session(
    *,
    session_id: str = "se-1",
    tool_call_id: str = "tc-1",
    event_key: str = "subscribe_to_trigger:tc-1",
    parked_status: str | None = "parked",
) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.WAITING,
        turn_status="idle",
        parked_status=parked_status,  # type: ignore[arg-type]
        parked_event_key=event_key,
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {
                "tool_name": "subscribe_to_trigger",
                "event_key": event_key,
            },
        },
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_respond_publishes_payload_onto_event_key(
    fake_storage_provider,
):
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(_parked_session())
    bus = _FakeEventBus()
    deps = RespondToYieldDeps(
        storage_provider=fake_storage_provider, event_bus=bus,
    )

    await respond_to_yield(
        session_id="se-1",
        tool_call_id="tc-1",
        result={"ok": True, "payload": {"hello": "world"}},
        deps=deps,
    )

    assert bus.published == [
        ("subscribe_to_trigger:tc-1",
         {"ok": True, "payload": {"hello": "world"}}),
    ]


@pytest.mark.asyncio
async def test_respond_wraps_non_dict_result(fake_storage_provider):
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(_parked_session())
    bus = _FakeEventBus()
    deps = RespondToYieldDeps(
        storage_provider=fake_storage_provider, event_bus=bus,
    )

    await respond_to_yield(
        session_id="se-1", tool_call_id="tc-1",
        result="raw string", deps=deps,
    )

    assert bus.published == [
        ("subscribe_to_trigger:tc-1", {"response": "raw string"}),
    ]


@pytest.mark.asyncio
async def test_respond_404_when_session_missing(fake_storage_provider):
    deps = RespondToYieldDeps(
        storage_provider=fake_storage_provider, event_bus=_FakeEventBus(),
    )
    with pytest.raises(NotFoundError):
        await respond_to_yield(
            session_id="missing", tool_call_id="tc-1",
            result={}, deps=deps,
        )


@pytest.mark.asyncio
async def test_respond_404_when_not_parked(fake_storage_provider):
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(_parked_session(parked_status=None))
    deps = RespondToYieldDeps(
        storage_provider=fake_storage_provider, event_bus=_FakeEventBus(),
    )
    with pytest.raises(NotFoundError):
        await respond_to_yield(
            session_id="se-1", tool_call_id="tc-1",
            result={}, deps=deps,
        )


@pytest.mark.asyncio
async def test_respond_404_on_tool_call_id_mismatch(fake_storage_provider):
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(_parked_session(tool_call_id="tc-1"))
    deps = RespondToYieldDeps(
        storage_provider=fake_storage_provider, event_bus=_FakeEventBus(),
    )
    with pytest.raises(NotFoundError):
        await respond_to_yield(
            session_id="se-1", tool_call_id="tc-other",
            result={}, deps=deps,
        )


@pytest.mark.asyncio
async def test_respond_accepts_resumable_state(fake_storage_provider):
    """A row already flipped to 'resumable' still resolves — duplicate
    publishes are idempotent at the listener layer."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(_parked_session(parked_status="resumable"))
    bus = _FakeEventBus()
    deps = RespondToYieldDeps(
        storage_provider=fake_storage_provider, event_bus=bus,
    )
    await respond_to_yield(
        session_id="se-1", tool_call_id="tc-1",
        result={"ok": True}, deps=deps,
    )
    assert len(bus.published) == 1


# ---------------------------------------------------------------------------
# tool_wait_event_key (01a0518b review, mixed-park wake seam)
# ---------------------------------------------------------------------------


def test_tool_wait_event_key_chat_workspace_surface() -> None:
    """The chat/workspace surface's scoped ids all carry the "x" node
    segment (node_id=None convention) - collapses to one key per turn."""
    assert (
        tool_wait_event_key("s1", 3, scoped_task_id="x:tool:3:1")
        == "tool_wait:s1:3:x"
    )


def test_tool_wait_event_key_graph_surface_is_node_qualified() -> None:
    """A real graph node id (not "x") produces a DIFFERENT key - two
    concurrent fan-out siblings' batches must never collide."""
    assert (
        tool_wait_event_key("s1", 3, scoped_task_id="workerNode:tool:3:2")
        == "tool_wait:s1:3:workerNode"
    )
    assert (
        tool_wait_event_key("s1", 3, scoped_task_id="A:tool:3:1")
        != tool_wait_event_key("s1", 3, scoped_task_id="B:tool:3:1")
    )


def test_tool_wait_event_key_fanout_instance_id_preserved() -> None:
    """A fan-out instance id ("worker[0]") is not itself colon-delimited,
    so splitting on the FIRST colon correctly keeps it whole."""
    assert (
        tool_wait_event_key("s1", 0, scoped_task_id="worker[0]:tool:0:1")
        == "tool_wait:s1:0:worker[0]"
    )


def test_tool_wait_event_key_same_batch_same_key() -> None:
    """Every id in the SAME batch shares the same node segment - any
    sibling can reconstruct the identical key, matching
    ToolCallTask.batch_task_ids' own "same value on every row" shape."""
    assert (
        tool_wait_event_key("s1", 0, scoped_task_id="A:tool:0:1")
        == tool_wait_event_key("s1", 0, scoped_task_id="A:tool:0:2")
    )
