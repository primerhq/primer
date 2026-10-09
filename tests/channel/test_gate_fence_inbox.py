"""A channel reply names the gate it answers, like the REST respond routes (console review C-033, ticket 01a11f52-9d98).

A provider repeats its tool_call_id across rounds, so an Approve button left in a chat from round 1 decided whatever gate was pending under the
same id later. The prompt envelope now carries the gate's ``gate_id``; each platform keeps it with the button (a ``#<token>`` suffix on the
button value or custom id, the Telegram tag cache) and the click's ``ResponseEnvelope`` brings it back. ``ChannelInbox`` judges it against the
pending gate: a reply naming a gate that is no longer the pending one is refused as stale (``StaleGateError``, nothing published, no record), a
reply naming none is still decided (an old button in a chat) and counted. A platform that can carry only a prefix (Discord's 100-character custom
id) sends the first 12 characters, which match the gate by prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.adapter import APPROVAL_ROUTED_NOTICE, QUESTION_STALE_NOTICE, ResponseEnvelope
from primer.channel.gate_tag import attach_gate_suffix, split_gate_suffix
from primer.channel.inbox import ChannelInbox
from primer.channel.null_adapter import NullChannelAdapter
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.approvers import ApproverRefusedError
from primer.session.gate_token import StaleGateError
from primer.worker.yield_runtime import _build_prompt_envelope
from tests.api.test_gate_fence import G1, G2, _ask_user_session, _graph_two_gate_session
from tests.conftest import _FakeStorageProvider


def _approval_session(session_id: str, *, gate_id: str | None) -> WorkspaceSession:
    now = datetime.now(UTC)
    ek = f"tool_approval:{session_id}:tc-1"
    metadata: dict = {"original_call": {"id": "tc-1", "name": "delete_workspace", "arguments": {"id": "ws-x"}}}
    if gate_id is not None:
        metadata["gate_id"] = gate_id
    return WorkspaceSession(
        id=session_id, workspace_id="ws-g", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=ek,
        parked_state={"tool_call_id": "tc-1", "yielded": {"tool_name": "_approval", "event_key": ek, "resume_metadata": metadata}},
    )


def _reply(session_id: str, *, gate_id: str | None, tool_call_id: str = "tc-1", kind: str = "tool_approval") -> ResponseEnvelope:
    return ResponseEnvelope(
        kind=kind, workspace_id="ws-g", session_id=session_id, tool_call_id=tool_call_id,
        response="EUR" if kind == "ask_user" else None, decision=None if kind == "ask_user" else "approved", reason=None,
        platform_metadata={"slack_user_id": "U1"}, gate_id=gate_id,
    )


class _World:
    def __init__(self, sp, inbox, sub) -> None:
        self.sp, self.inbox, self.sub = sp, inbox, sub

    async def published(self, timeout: float = 0.3):
        try:
            return await asyncio.wait_for(anext(self.sub), timeout=timeout)
        except TimeoutError:
            return None

    async def records(self) -> list:
        return (await self.sp.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=20))).items


@pytest.fixture
async def world():
    bus = InMemoryEventBus()
    await bus.initialize()
    sp = _FakeStorageProvider()
    sub = bus.subscribe()
    try:
        yield _World(sp, ChannelInbox(event_bus=bus, storage_provider=sp), sub)
    finally:
        await sub.aclose()
        await bus.aclose()


@pytest.fixture(autouse=True)
def _fresh_metrics():
    import primer.observability.metrics as m

    m.reset_for_test()
    yield
    m.reset_for_test()


def _count(kind: str, token: str) -> float:
    import primer.observability.metrics as m

    return m.registry.get_sample_value("gate_respond_total", {"kind": kind, "gate_token": token}) or 0.0


# ---- the tag helpers -------------------------------------------------------------------------------------------------------------------------


def test_a_gate_suffix_round_trips_and_leaves_a_plain_id_alone() -> None:
    assert attach_gate_suffix("tc-1", G1) == f"tc-1#{G1}"
    assert split_gate_suffix(f"tc-1#{G1}") == ("tc-1", G1)
    assert split_gate_suffix(f"tc-1#{G1[:12]}") == ("tc-1", G1[:12])
    assert split_gate_suffix("tc-1") == ("tc-1", None)
    assert attach_gate_suffix("tc-1", None) == "tc-1"


def test_only_a_trailing_hex_token_is_taken_for_a_gate() -> None:
    """A provider id may hold a '#'; only a '#' followed by exactly 12 or 32 lowercase hex characters ends the id."""
    assert split_gate_suffix("call#7") == ("call#7", None)
    assert split_gate_suffix("call#" + "a" * 13) == ("call#" + "a" * 13, None)
    assert split_gate_suffix("call#" + "A" * 32) == ("call#" + "A" * 32, None)
    assert split_gate_suffix("a#b#" + "c" * 12) == ("a#b", "c" * 12)


def test_a_suffix_that_would_pass_the_platforms_length_limit_is_left_off() -> None:
    long_id = "x" * 80
    assert attach_gate_suffix(long_id, G1[:12], max_len=100, base_len=10) == long_id
    assert attach_gate_suffix("tc", G1[:12], max_len=100, base_len=10) == "tc#" + G1[:12]


# ---- approvals -------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [G1, G1[:12]], ids=["full-id", "short-token"])
async def test_a_reply_naming_the_pending_gate_decides_it(world, token) -> None:
    await world.sp.get_storage(WorkspaceSession).create(_approval_session("i-ok", gate_id=G1))

    await world.inbox.handle_response(_reply("i-ok", gate_id=token))

    event = await world.published()
    assert event is not None and event.event_key == "tool_approval:i-ok:tc-1"
    assert _count("approval", "matched") == 1


@pytest.mark.asyncio
async def test_the_channel_decision_record_is_keyed_by_the_gate(world) -> None:
    """C-033 PR 2: the audit record's key names the gate (``<event_key>@<gate_id>``), so a later gate under the same raw id keeps its own record."""
    await world.sp.get_storage(WorkspaceSession).create(_approval_session("i-key", gate_id=G1))

    await world.inbox.handle_response(_reply("i-key", gate_id=G1[:12]))

    assert [r.gate_event_key for r in await world.records()] == [f"tool_approval:i-key:tc-1@{G1}"]


@pytest.mark.asyncio
async def test_a_reply_naming_a_replaced_gate_is_refused_as_stale_and_decides_nothing(world) -> None:
    """Round 1's Approve button, clicked after round 3 parked the same raw id under another gate."""
    await world.sp.get_storage(WorkspaceSession).create(_approval_session("i-stale", gate_id=G2))

    with pytest.raises(StaleGateError):
        await world.inbox.handle_response(_reply("i-stale", gate_id=G1))

    assert await world.published() is None, "a stale click reached the event bus and decided the new gate"
    assert await world.records() == []
    assert _count("approval", "stale") == 1


@pytest.mark.asyncio
async def test_a_reply_naming_no_gate_is_still_decided_and_counted(world) -> None:
    """A button posted before this release carries no token."""
    await world.sp.get_storage(WorkspaceSession).create(_approval_session("i-bare", gate_id=G1))

    await world.inbox.handle_response(_reply("i-bare", gate_id=None))

    assert (await world.published()) is not None
    assert _count("approval", "absent") == 1


@pytest.mark.asyncio
async def test_a_token_for_a_gate_with_no_stamped_id_is_stale(world) -> None:
    await world.sp.get_storage(WorkspaceSession).create(_approval_session("i-legacy", gate_id=None))

    with pytest.raises(StaleGateError):
        await world.inbox.handle_response(_reply("i-legacy", gate_id=G1))


@pytest.mark.asyncio
async def test_each_gate_of_a_multi_gate_park_is_fenced_by_its_own_token(world) -> None:
    await world.sp.get_storage(WorkspaceSession).create(_graph_two_gate_session(session_id="i-multi"))

    with pytest.raises(StaleGateError):
        await world.inbox.handle_response(_reply("i-multi", gate_id=G2, tool_call_id="call-0"))
    assert await world.published() is None

    await world.inbox.handle_response(_reply("i-multi", gate_id=G2[:12], tool_call_id="call-1"))
    event = await world.published()
    assert event is not None and event.event_key == "tool_approval:i-multi:call-1"


@pytest.mark.asyncio
async def test_a_matching_token_does_not_bypass_the_approver_check(world) -> None:
    session = _approval_session("i-appr", gate_id=G1)
    session.parked_state["yielded"]["resume_metadata"]["approvers"] = {"kind": "users", "users": ["alice"]}
    await world.sp.get_storage(WorkspaceSession).create(session)

    with pytest.raises(ApproverRefusedError):
        await world.inbox.handle_response(_reply("i-appr", gate_id=G1))
    assert await world.published() is None
    assert _count("approval", "matched") == 0, "a decision the approver check refused was not a decision that named the right gate"


# ---- ask_user ---------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_to_the_pending_question_answers_it(world) -> None:
    await world.sp.get_storage(WorkspaceSession).create(_ask_user_session(session_id="q-ok", tool_call_id="tc-1", gate_id=G1))

    await world.inbox.handle_response(_reply("q-ok", gate_id=G1, kind="ask_user"))

    event = await world.published()
    assert event is not None and event.event_key == "ask_user:q-ok:tc-1"
    assert event.payload == {"response": "EUR", "__yield_gate_id__": G1}, "the answer, plus the gate it resolved (C-033 PR 4)"
    assert _count("ask_user", "matched") == 1


@pytest.mark.asyncio
async def test_a_reply_to_a_replaced_question_is_refused_as_stale_and_answers_nothing(world) -> None:
    await world.sp.get_storage(WorkspaceSession).create(_ask_user_session(session_id="q-stale", tool_call_id="tc-1", gate_id=G2))

    with pytest.raises(StaleGateError):
        await world.inbox.handle_response(_reply("q-stale", gate_id=G1, kind="ask_user"))

    assert await world.published() is None
    assert _count("ask_user", "stale") == 1


@pytest.mark.asyncio
async def test_a_reply_naming_no_question_answers_the_pending_one_and_is_counted_as_absent(world) -> None:
    """A thread reply whose correlation row predates gate ids has no token: it still answers what is pending in the thread, and is counted, so the
    flip to refusing tokenless replies can be scheduled from one number (round 2 of the C-033 review)."""
    await world.sp.get_storage(WorkspaceSession).create(_ask_user_session(session_id="q-bare", tool_call_id="tc-1", gate_id=G1))

    await world.inbox.handle_response(_reply("q-bare", gate_id=None, kind="ask_user"))

    assert (await world.published()) is not None
    assert _count("ask_user", "absent") == 1


@pytest.mark.asyncio
async def test_a_tokenless_reply_to_nothing_pending_is_not_counted(world) -> None:
    """Only a reply that reaches a pending prompt is a decision attempt worth counting."""
    await world.sp.get_storage(WorkspaceSession).create(_ask_user_session(session_id="q-none", tool_call_id="tc-other", gate_id=G1))

    with contextlib.suppress(Exception):   # whatever the lookup that follows does with it, this test is about the counter
        await world.inbox.handle_response(_reply("q-none", gate_id=None, kind="ask_user"))

    assert _count("ask_user", "absent") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["agent", "tool_call"])
async def test_a_reply_to_the_second_of_two_sibling_questions_is_published_to_that_questions_own_key(world, via) -> None:
    """Two graph nodes ask under one raw id with node-scoped keys. The fence judged the gate the reply named, but the key it was published to was the FIRST
    sibling's: the lookup that follows matched by the raw id alone and took the first (round 2 of the C-033 review, PR 3)."""
    from tests.api.test_gate_fence_round2 import _graph_ask_user_session

    await world.sp.get_storage(WorkspaceSession).create(_graph_ask_user_session(session_id="q-sib", via=via))

    await world.inbox.handle_response(_reply("q-sib", gate_id=G2, kind="ask_user", tool_call_id="dup"))

    event = await world.published()
    assert event is not None and event.event_key == "ask_user:q-sib:n1:dup", event


@pytest.mark.asyncio
async def test_the_published_decision_names_the_gate_it_resolved(world) -> None:
    """The wake of a channel decision carries the id of the gate it resolved (named or not), so a redelivery cannot decide a later gate (PR 4)."""
    await world.sp.get_storage(WorkspaceSession).create(_approval_session("i-wake", gate_id=G1))

    await world.inbox.handle_response(_reply("i-wake", gate_id=None))

    event = await world.published()
    assert event is not None and event.payload["__yield_gate_id__"] == G1 and event.payload["decision"] == "approved"


@pytest.mark.asyncio
async def test_the_published_answer_names_the_question_it_resolved(world) -> None:
    await world.sp.get_storage(WorkspaceSession).create(_ask_user_session(session_id="q-wake", tool_call_id="tc-1", gate_id=G1))

    await world.inbox.handle_response(_reply("q-wake", gate_id=G1, kind="ask_user"))

    event = await world.published()
    assert event is not None and event.payload["__yield_gate_id__"] == G1 and event.payload["response"] == "EUR"


# ---- the adapters' shared relay ---------------------------------------------------------------------------------------------------------------


class _Inbox:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.envelopes: list[ResponseEnvelope] = []

    async def handle_response(self, env) -> None:
        self.envelopes.append(env)
        if self.error is not None:
            raise self.error


class _Adapter(NullChannelAdapter):
    def __init__(self, inbox: _Inbox) -> None:
        super().__init__()
        self._inbox = inbox


async def _decide(adapter: _Adapter, **extra):
    return await adapter._handle_decision(
        workspace_id="ws", session_id="s", tool_call_id="tc", decision="approved", reason=None, user_id="U1", **extra,
    )


@pytest.mark.asyncio
async def test_the_relay_carries_the_gate_id_onto_the_envelope() -> None:
    inbox = _Inbox()
    adapter = _Adapter(inbox)

    assert await _decide(adapter, gate_id=G1) is True
    await adapter._handle_text_reply(workspace_id="ws", session_id="s", tool_call_id="tc", text="EUR", user_id="U1", gate_id=G2)

    assert [e.gate_id for e in inbox.envelopes] == [G1, G2]


@pytest.mark.asyncio
async def test_a_stale_click_reports_a_refusal_whose_notice_says_the_approval_was_replaced() -> None:
    from primer.channel import adapter as adapter_module

    refused = await _decide(_Adapter(_Inbox(StaleGateError("approval"))), gate_id=G1)

    assert not refused
    assert "replaced" in refused.notice and refused.notice != APPROVAL_ROUTED_NOTICE
    assert len(refused.notice) <= 200, "Telegram's alert on a callback query takes no more than 200 characters"
    assert refused.notice == adapter_module.APPROVAL_STALE_NOTICE


@pytest.mark.asyncio
async def test_a_stale_ask_user_reply_reports_a_refusal_whose_notice_says_the_question_was_replaced() -> None:
    refused = await _Adapter(_Inbox(StaleGateError("ask_user")))._handle_text_reply(
        workspace_id="ws", session_id="s", tool_call_id="tc", text="EUR", user_id="U1", gate_id=G1,
    )

    assert not refused and refused.notice == QUESTION_STALE_NOTICE and "replaced" in refused.notice
    assert len(refused.notice) <= 200


@pytest.mark.asyncio
async def test_an_approver_refusal_still_reports_the_routed_notice() -> None:
    refused = await _decide(_Adapter(_Inbox(ApproverRefusedError("routed"))))

    assert not refused and refused.notice == APPROVAL_ROUTED_NOTICE


# ---- the prompt envelope ----------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["_approval", "ask_user"])
def test_the_prompt_envelope_carries_the_gate_id_its_gate_was_minted_with(kind) -> None:
    metadata = {"gate_id": G1, "prompt": "?", "original_call": {"id": "tc", "name": "write", "arguments": {}}}

    env = _build_prompt_envelope(kind=kind, workspace_id="w", session_id="s", fallback_tool_call_id="tc", metadata=metadata)

    assert env is not None and env.gate_id == G1
    assert _build_prompt_envelope(
        kind=kind, workspace_id="w", session_id="s", fallback_tool_call_id="tc", metadata={"prompt": "?"},
    ).gate_id is None
