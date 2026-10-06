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
        tool_wait_event_key("s1", scoped_task_id="x:tool:3:1")
        == "tool_wait:s1:3:x"
    )


def test_tool_wait_event_key_graph_surface_is_node_qualified() -> None:
    """A real graph node id (not "x") produces a DIFFERENT key - two
    concurrent fan-out siblings' batches must never collide."""
    assert (
        tool_wait_event_key("s1", scoped_task_id="workerNode:tool:3:2")
        == "tool_wait:s1:3:workerNode"
    )
    assert (
        tool_wait_event_key("s1", scoped_task_id="A:tool:3:1")
        != tool_wait_event_key("s1", scoped_task_id="B:tool:3:1")
    )


def test_tool_wait_event_key_fanout_instance_id_preserved() -> None:
    """Pinned regression: a fan-out instance id ("worker[0]") is kept whole."""
    assert (
        tool_wait_event_key("s1", scoped_task_id="worker[0]:tool:0:1")
        == "tool_wait:s1:0:worker[0]"
    )


def test_tool_wait_event_key_same_batch_same_key() -> None:
    """Every id in the SAME batch shares the same node segment - any
    sibling can reconstruct the identical key, matching
    ToolCallTask.batch_task_ids' own "same value on every row" shape."""
    assert (
        tool_wait_event_key("s1", scoped_task_id="A:tool:0:1")
        == tool_wait_event_key("s1", scoped_task_id="A:tool:0:2")
    )


def test_tool_wait_event_key_is_the_same_for_the_scoped_and_the_session_qualified_id() -> None:
    """S1b: a task id is `<session_id>/<scoped>`. The key's node segment comes from the SCOPED part, so every site
    gets the same key whichever form it holds (mutation: split the qualified id as is, node segment = "s1/x")."""
    for scoped in ("x:tool:3:1", "workerNode:tool:3:2", "worker[0]:tool:0:1", "a:b:tool:3:1", "a.b:tool:3.2:1"):
        assert (
            tool_wait_event_key("s1", scoped_task_id=f"s1/{scoped}")
            == tool_wait_event_key("s1", scoped_task_id=scoped)
        )
    assert tool_wait_event_key("s1", scoped_task_id="s1/x:tool:3:1") == "tool_wait:s1:3:x"


def test_tool_wait_event_key_does_not_strip_another_sessions_prefix() -> None:
    """Only the exact `<this session>/` prefix is the qualification; a node id that merely contains a slash is not."""
    assert tool_wait_event_key("s1", scoped_task_id="a/b:tool:0:1") == "tool_wait:s1:0:a/b"


def test_tool_wait_event_key_keeps_the_full_node_id_so_nodes_sharing_a_prefix_do_not_collide() -> None:
    """Graph node ids are free-form. Splitting at the FIRST colon collapsed nodes ``a:b`` and ``a:c`` onto one key
    ``...:a``, so one batch's wake woke the other's."""
    key_b = tool_wait_event_key("s1", scoped_task_id="a:b:tool:3:1")
    key_c = tool_wait_event_key("s1", scoped_task_id="s1/a:c:tool:3:1")
    assert (key_b, key_c) == ("tool_wait:s1:3:a:b", "tool_wait:s1:3:a:c")
    assert tool_wait_event_key("s1", scoped_task_id="s1/a:b:tool:5:1") == "tool_wait:s1:5:a:b"


@pytest.mark.parametrize("turn_seg", ["0", "1", "3", "42", "3.1", "3.2", "10.7"])
@pytest.mark.parametrize("node", ["x", "a:b", "worker[0]"])
def test_tool_wait_event_key_depends_only_on_the_id(turn_seg: str, node: str) -> None:
    """The key is a pure function of the id: the turn segment is the one the id was minted with, as written, so the
    same id gives the same key whatever turn the session has moved on to (mutation N27, pure leg: take the turn
    from a ``turn_no`` argument). The function takes no turn at all."""
    import inspect

    assert list(inspect.signature(tool_wait_event_key).parameters) == ["session_id", "scoped_task_id"]
    scoped = f"{node}:tool:{turn_seg}:1"
    assert tool_wait_event_key("s1", scoped_task_id=scoped) == f"tool_wait:s1:{turn_seg}:{node}"
    assert tool_wait_event_key("s1", scoped_task_id=f"s1/{scoped}") == f"tool_wait:s1:{turn_seg}:{node}"


def test_tool_wait_event_key_keeps_the_epoch() -> None:
    """The parser accepts a ``<turn>.<epoch>`` turn segment (nothing writes one yet), so the key of such an id must
    not be the key of the retired turn's batch (mutation N46, pure leg: drop the epoch segment)."""
    assert tool_wait_event_key("s1", scoped_task_id="x:tool:3:1") == "tool_wait:s1:3:x"
    assert tool_wait_event_key("s1", scoped_task_id="x:tool:3.1:1") == "tool_wait:s1:3.1:x"


def test_tool_wait_event_key_raises_on_a_malformed_id() -> None:
    from primer.model.tool_call_task import MalformedScopedIdError

    for bad in ("x:tool:03:1", "s1/x:tool:3:0", ":tool:3:1", "x:call:3:1"):
        with pytest.raises(MalformedScopedIdError):
            tool_wait_event_key("s1", scoped_task_id=bad)


# ---------------------------------------------------------------------------
# _dispatch_key_for: the resume_event_payloads leaf key
# ---------------------------------------------------------------------------


def test_dispatch_key_for_a_tool_wait_key_is_the_key_itself() -> None:
    from primer.session.yields import _dispatch_key_for

    for key in ("tool_wait:s1:3:x", "tool_wait:s1:5:a:b", "tool_wait:s1:3.1:worker[0]"):
        assert _dispatch_key_for(key, session_id="s1") == key


def test_dispatch_key_for_human_gate_keys_is_unchanged() -> None:
    from primer.session.yields import _dispatch_key_for

    assert _dispatch_key_for("ask_user:s1:tc-1", session_id="s1") == "tc-1"
    assert _dispatch_key_for("tool_approval:s1:tc-1", session_id="s1") == "tc-1"
    assert _dispatch_key_for("tool_approval:s1:nodeA:tc-1", session_id="s1") == "nodeA:tc-1"
    assert _dispatch_key_for("ask_user:other:tc-1", session_id="s1") == "ask_user:other:tc-1"


@pytest.mark.asyncio
async def test_a_tool_wait_wake_and_a_human_gate_reply_with_the_same_tail_keep_two_leaves(
    fake_storage_provider,
) -> None:
    """A human gate on node ``5`` with tool_call_id ``a:b`` and a tool_wait batch of node ``a:b`` in turn 5 both have
    the tail ``5:a:b``. Stripping both to the tail made one leaf, and the second write overwrote the human reply
    (mutation N69: ``_dispatch_key_for`` keeps stripping the ``tool_wait:<sid>:`` prefix)."""
    from primer.session.yields import durably_mark_session_resumable

    gate_key = "tool_approval:se-1:5:a:b"
    wait_key = "tool_wait:se-1:5:a:b"   # tool_wait_event_key of "se-1/a:b:tool:5:1" (pinned above)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    session = _parked_session(event_key=gate_key).model_copy(update={"parked_event_keys": [gate_key, wait_key]})
    await sessions.create(session)

    for key, payload in ((gate_key, {"decision": "approved"}), (wait_key, {"tool_wait_ready": True})):
        fresh = await sessions.get("se-1")
        assert await durably_mark_session_resumable(
            fresh, event_key=key, payload=payload, session_storage=sessions, engine=None,
        )

    leaves = (await sessions.get("se-1")).parked_state["resume_event_payloads"]
    assert len(leaves) == 2, leaves
    assert sorted(entry["event_key"] for entry in leaves.values()) == sorted([gate_key, wait_key])
    by_key = {entry["event_key"]: entry["payload"] for entry in leaves.values()}
    assert by_key[gate_key] == {"decision": "approved"}, "the human reply was overwritten"
