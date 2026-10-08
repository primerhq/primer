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
