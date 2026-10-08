"""A channel reply honours the gate's approver spec (ticket 01a11b64).

``ChannelInbox.handle_response`` published any ``tool_approval`` reply straight onto the event bus: it never looked at the gate's stamped
``approvers``, so a Slack/Discord/Telegram user could approve what the policy restricted to alice or to admins (including the admin-only
fallback for duplicate policies). A chat-platform user is not a primer user (the envelope carries only a platform id), so it is an
UNIDENTIFIED decider: it may answer an unrestricted gate and is refused on a restricted one, which is then decided in the console. The rule
is ``primer.session.approvers.may_decide``, the same function the REST respond route uses.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.adapter import ResponseEnvelope
from primer.channel.inbox import ChannelInbox
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.approvers import ApproverRefusedError
from tests.api.test_approver_routing import _two_gate_graph_session_with_different_approvers
from tests.conftest import _FakeStorageProvider


def _parked(session_id: str, *, approvers: dict | None) -> WorkspaceSession:
    now = datetime.now(UTC)
    event_key = f"tool_approval:{session_id}:tc-1"
    metadata: dict = {"original_call": {"id": "tc-1", "name": "delete_workspace", "arguments": {"id": "ws-x"}}}
    if approvers is not None:
        metadata["approvers"] = approvers
    return WorkspaceSession(
        id=session_id, workspace_id="ws-1", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=event_key,
        parked_state={
            "tool_call_id": "tc-1",
            "yielded": {"tool_name": "_approval", "event_key": event_key, "resume_metadata": metadata},
        },
    )


def _reply(session_id: str, *, decision: str = "approved", tool_call_id: str = "tc-1") -> ResponseEnvelope:
    return ResponseEnvelope(
        kind="tool_approval", workspace_id="ws-1", session_id=session_id, tool_call_id=tool_call_id,
        response=None, decision=decision, reason=None, platform_metadata={"slack_user_id": "U123"},
    )


class _Setup:
    def __init__(self, sp, bus, inbox, sub) -> None:
        self.sp, self.bus, self.inbox, self.sub = sp, bus, inbox, sub

    async def published(self, timeout: float = 0.3):
        try:
            return await asyncio.wait_for(anext(self.sub), timeout=timeout)
        except TimeoutError:
            return None


@pytest.fixture
async def world():
    bus = InMemoryEventBus()
    await bus.initialize()
    sp = _FakeStorageProvider()
    sub = bus.subscribe()
    try:
        yield _Setup(sp, bus, ChannelInbox(event_bus=bus, storage_provider=sp), sub)
    finally:
        await sub.aclose()
        await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected"])
@pytest.mark.parametrize(
    "approvers",
    [
        {"kind": "users", "users": ["alice"]},
        {"kind": "roles", "roles": ["ops"]},
        {"kind": "roles", "roles": []},
    ],
    ids=["users-alice", "roles-ops", "admin-only"],
)
async def test_a_channel_reply_cannot_decide_a_restricted_gate(world, approvers, decision) -> None:
    await world.sp.get_storage(WorkspaceSession).create(_parked("s-r", approvers=approvers))

    with pytest.raises(ApproverRefusedError):
        await world.inbox.handle_response(_reply("s-r", decision=decision))

    assert await world.published() is None, "a refused reply reached the event bus and would have decided the gate"
    records = await world.sp.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=10))
    assert records.items == [], "a refused reply left a decision record"


@pytest.mark.asyncio
@pytest.mark.parametrize("approvers", [None, {"kind": "anyone"}], ids=["no-spec", "anyone"])
async def test_a_channel_reply_still_decides_an_unrestricted_gate(world, approvers) -> None:
    await world.sp.get_storage(WorkspaceSession).create(_parked("s-u", approvers=approvers))

    await world.inbox.handle_response(_reply("s-u"))

    event = await world.published()
    assert event is not None and event.event_key == "tool_approval:s-u:tc-1"
    assert event.payload == {"decision": "approved", "reason": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_call_id", ["call-0", "call-1"])
async def test_each_gate_of_a_graph_park_is_judged_by_its_own_spec(world, tool_call_id) -> None:
    """A fan-out park with two gates, routed to alice and to bob: a channel reply is neither, on either."""
    await world.sp.get_storage(WorkspaceSession).create(_two_gate_graph_session_with_different_approvers(session_id="s-g"))

    with pytest.raises(ApproverRefusedError):
        await world.inbox.handle_response(_reply("s-g", tool_call_id=tool_call_id))

    assert await world.published() is None


@pytest.mark.asyncio
async def test_a_reply_for_a_session_that_does_not_exist_is_published_as_before(world) -> None:
    """The chat surface has no WorkspaceSession row: nothing to enforce, and its flow is unchanged."""
    await world.inbox.handle_response(_reply("chat-1"))

    event = await world.published()
    assert event is not None and event.event_key == "tool_approval:chat-1:tc-1"


# ---- each gate by its OWN spec, and the publish bound to the gate that was checked (follow-up of #536) -----------------------------


def _gate_metadata(tool_call_id: str, approvers: dict | None) -> dict:
    return {
        "policy_id": "pol", "approval_type": "required", "gate_reason": None, "approvers": approvers,
        "original_call": {"id": tool_call_id, "name": "delete_workspace", "arguments": {}},
    }


def _graph_park(session_id: str, *, toolcalls: list[tuple[str, str, dict | None]], agent_yields: list[tuple[str, str, dict | None]] = ()) -> WorkspaceSession:
    """A graph park whose checkpoint holds the given gates: ``(tool_call_id, event_key, approvers)`` in ``pending_toolcalls`` (a ToolCall
    node's gate, which production also writes to ``pending_dispatch`` as ``original_call`` only) and in ``pending_agent_yields`` (an agent
    node's ``_approval`` yield, which is NOT stored in ``pending_dispatch``)."""
    now = datetime.now(UTC)
    calls = [
        {
            "node_id": f"node-{tcid}", "tool_call_id": tcid, "parked_event_key": key, "arguments": {}, "tool_name": "_approval",
            "resume_metadata": _gate_metadata(tcid, approvers), "scoped_tool_call_id": None,
        }
        for tcid, key, approvers in toolcalls
    ]
    yields = [
        {
            "node_id": f"agent-{tcid}", "tool_call_id": tcid, "event_key": key, "tool_name": "_approval",
            "resume_metadata": _gate_metadata(tcid, approvers),
        }
        for tcid, key, approvers in agent_yields
    ]
    primary = "primary-gate"
    return WorkspaceSession(
        id=session_id, workspace_id="ws-1", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=f"tool_approval:{session_id}:{primary}",
        parked_state={
            "tool_call_id": primary,
            "yielded": {"tool_name": "_approval", "event_key": f"tool_approval:{session_id}:{primary}", "resume_metadata": {}},
            "graph_checkpoint": {
                "pending_toolcalls": calls,
                "pending_agent_yields": yields,
                "pending_dispatch": [
                    {
                        "kind": "_approval", "node_id": c["node_id"], "tool_call_id": c["tool_call_id"],
                        "resume_metadata": {"original_call": c["resume_metadata"]["original_call"]},
                    }
                    for c in calls
                ],
            },
        },
    )


@pytest.mark.asyncio
async def test_a_reply_to_an_unrestricted_gate_is_published_while_its_restricted_sibling_is_refused(world) -> None:
    """One park, two gates: call-0 routed to alice, call-1 to anyone. Judging every gate by the PRIMARY's spec (call-0's) would refuse
    call-1; judging by the last, or by none, would let call-0 through. Each reply is judged by its own gate."""
    await world.sp.get_storage(WorkspaceSession).create(_graph_park("s-m", toolcalls=[
        ("call-0", "tool_approval:s-m:call-0", {"kind": "users", "users": ["alice"]}),
        ("call-1", "tool_approval:s-m:call-1", {"kind": "anyone"}),
    ]))

    with pytest.raises(ApproverRefusedError):
        await world.inbox.handle_response(_reply("s-m", tool_call_id="call-0"))
    assert await world.published() is None

    await world.inbox.handle_response(_reply("s-m", tool_call_id="call-1"))
    event = await world.published()
    assert event is not None and event.event_key == "tool_approval:s-m:call-1"


@pytest.mark.asyncio
async def test_the_reply_is_published_to_the_event_key_of_the_gate_that_was_checked(world) -> None:
    """Two gates share a raw tool_call_id (two fan-out siblings can): a ToolCall gate that anyone may decide and an agent node's
    ``_approval`` yield restricted to alice. The reply is judged against ONE entry and must be published to THAT entry's own key; the
    inbox used to check one entry and publish to the key of another (the first by a second matcher), so a reply admitted by the open
    gate could wake the restricted one."""
    await world.sp.get_storage(WorkspaceSession).create(_graph_park(
        "s-b",
        toolcalls=[("dup", "tool_approval:s-b:open-node:dup", {"kind": "anyone"})],
        agent_yields=[("dup", "tool_approval:s-b:restricted-node:dup", {"kind": "users", "users": ["alice"]})],
    ))

    await world.inbox.handle_response(_reply("s-b", tool_call_id="dup"))

    event = await world.published()
    assert event is not None
    assert event.event_key == "tool_approval:s-b:open-node:dup", (
        f"the reply was admitted by the open gate but published to {event.event_key!r}"
    )


# ---- a reply is judged by a gate it can read, or refused (follow-up of #540 review) -----------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_is_refused_when_the_checkpoint_names_the_gate_but_holds_no_entry_whose_spec_can_be_read(world) -> None:
    """``pending_dispatch`` names an ``_approval`` gate for the tool_call_id but ``pending_toolcalls`` has no entry for it (an
    inconsistent checkpoint). The reply used to fall through to the event-key lookup, which matched the ``pending_dispatch`` entry and
    published with NO check, though the spec of that gate cannot be known. It cannot be shown to be unrestricted, so it is refused."""
    park = _graph_park("s-d", toolcalls=[])
    park.parked_state["graph_checkpoint"]["pending_dispatch"] = [
        {"kind": "_approval", "node_id": "worker", "tool_call_id": "tc-1", "resume_metadata": {"original_call": {"id": "tc-1", "name": "x", "arguments": {}}}},
    ]
    await world.sp.get_storage(WorkspaceSession).create(park)

    with pytest.raises(ApproverRefusedError):
        await world.inbox.handle_response(_reply("s-d"))

    assert await world.published() is None, "a reply for a gate whose spec could not be read reached the event bus"


@pytest.mark.asyncio
async def test_a_park_with_no_tool_name_is_not_matched_as_an_approval_gate(world) -> None:
    """The event-key lookup used to accept a park whose ``tool_name`` is ``None`` as an approval gate (a "legacy" shape nothing writes
    today). The gate resolver never reads such an entry, so the reply was published to that park's own key with no check. A nameless
    park is not an approval gate: the reply goes to the reconstructed key and never to the key the nameless park carries."""
    row = _parked("s-n", approvers={"kind": "users", "users": ["alice"]})
    row.parked_state["yielded"]["tool_name"] = None
    row.parked_state["yielded"]["event_key"] = "nameless-park-key"
    await world.sp.get_storage(WorkspaceSession).create(row)

    await world.inbox.handle_response(_reply("s-n"))

    event = await world.published()
    assert event is not None
    assert event.event_key == "tool_approval:s-n:tc-1", f"the reply was matched to a nameless park and published to {event.event_key!r}"
