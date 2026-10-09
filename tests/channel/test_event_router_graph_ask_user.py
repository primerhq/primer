"""A routed thread reply reaches the ask_user it answers, however the platform scoped its key (console review C-033, #683 review round 2, B2).

``ChannelEventRouter``'s correlation-first branch used to reconstruct ``ask_user:{session}:{tool_call_id}`` and publish to it. An ask_user inside a graph agent
node waits on ``ask_user:{session}:{node}:{tool_call_id}`` since the node-scoping of PR 3, so the reply woke nothing, and the router cleared the correlation's
gate anyway: the answer was lost and the NEXT reply in the thread steered the session. The reply now goes through ``ChannelInbox.handle_response`` like every
adapter's direct reply: the stored event key is resolved (the one the pending gate waits on) and the C-033 stale fence applies, so a reply to a prompt that
has since been replaced is refused instead of answering the newer question.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.model.channel import Channel, ChannelProviderType, TelegramChannelConfig
from primer.model.channel_event import ChannelEvent, EventSender, NormalizedEventType
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.yields import flip_sessions_parked_on
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider

GS = "gs-1"
GA, GB = "a" * 32, "b" * 32
NODE_KEY = f"ask_user:{GS}:A:call_0"        # what _ask_user_handler builds inside graph node A since PR 3
FLAT_KEY = f"ask_user:{GS}:call_0"          # an ask_user outside a graph node


class _FlippingBus:
    def __init__(self, store) -> None:
        self.store, self.published, self.flipped = store, [], 0

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload))
        self.flipped += await flip_sessions_parked_on(event_key, payload or {}, session_storage=self.store, engine=None)


async def _setup(*, graph: bool, pending_gate: str | None, correlation_gate: str | None):
    sp = _FakeStorageProvider()
    store = sp.get_storage(WorkspaceSession)
    now = datetime.now(UTC)
    key = NODE_KEY if graph else FLAT_KEY
    meta = {"prompt": "q", **({"gate_id": pending_gate} if pending_gate else {})}
    state: dict = {"tool_call_id": "call_0", "yielded": {"tool_name": "_approval" if graph else "ask_user", "event_key": key, "resume_metadata": meta}}
    if graph:
        entry = {"node_id": "A", "tool_call_id": "call_0", "event_key": key, "tool_name": "ask_user", "resume_metadata": meta}
        state["graph_checkpoint"] = {"pending_toolcalls": [], "pending_agent_yields": [entry], "pending_dispatch": []}
    await store.create(WorkspaceSession(
        id=GS, workspace_id="w1", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING, created_at=now,
        parked_status="parked", parked_at=now, parked_event_key=key, parked_event_keys=[key], parked_state=state,
    ))
    await CorrelationStore(sp).upsert_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1", session_id=GS, tool_call_id="call_0", gate_id=correlation_gate,
    )
    bus = _FlippingBus(store)
    router = ChannelEventRouter(
        storage_provider=sp, correlation_store=CorrelationStore(sp), fire_deps=DispatchDeps(storage_provider=sp, claim_engine=None), event_bus=bus,
    )
    return sp, store, bus, router


async def _reply(router, sp, text: str = "blue"):
    now = datetime.now(UTC)
    channel = Channel(id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM, external_id="777", config=TelegramChannelConfig())
    event = ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-1", event_id="ev-1", type=NormalizedEventType.MESSAGE_POSTED, occurred_at=now,
        channel_id="ch-1", surface="thread", thread_anchor="thr-1", sender=EventSender(external_id="u1"), text=text,
    )
    outcome = await router.route_event(event=event, channel=channel)
    return outcome, (await CorrelationStore(sp).lookup("ch-1", "thr-1")).tool_call_id


@pytest.mark.asyncio
async def test_a_routed_thread_reply_reaches_a_graph_node_ask_user() -> None:
    sp, store, bus, router = await _setup(graph=True, pending_gate=GA, correlation_gate=GA)

    outcome, _gate_after = await _reply(router, sp)

    assert bus.flipped == 1, f"the answer was published to {[k for k, _ in bus.published]} and woke nothing"
    assert [k for k, _ in bus.published] == [NODE_KEY]
    assert outcome.kind == "gate" and (await store.get(GS)).parked_status == "resumable"


@pytest.mark.asyncio
async def test_the_published_answer_is_the_text_of_the_reply() -> None:
    sp, _store, bus, router = await _setup(graph=True, pending_gate=GA, correlation_gate=GA)

    await _reply(router, sp)

    [(_key, payload)] = bus.published
    assert payload["response"] == "blue"


@pytest.mark.asyncio
async def test_a_reply_to_a_prompt_that_was_replaced_does_not_answer_the_newer_question() -> None:
    """The thread's prompt was posted for gate GA; the session has since asked a NEW question (GB) under the same id."""
    sp, store, bus, router = await _setup(graph=True, pending_gate=GB, correlation_gate=GA)

    outcome, gate_after = await _reply(router, sp)

    assert bus.flipped == 0 and bus.published == [], "the old prompt's reply answered the newer question"
    assert (await store.get(GS)).parked_status != "resumable"
    # What the stale fence does with the text: the thread no longer waits on that prompt, so the correlation's gate is cleared and the text steers the session.
    assert outcome.kind == "steer" and gate_after in (None, "")


@pytest.mark.asyncio
async def test_a_correlation_row_from_before_gate_ids_still_routes_the_reply() -> None:
    sp, store, bus, router = await _setup(graph=True, pending_gate=GA, correlation_gate=None)

    outcome, _gate_after = await _reply(router, sp)

    assert bus.flipped == 1 and outcome.kind == "gate" and (await store.get(GS)).parked_status == "resumable"


@pytest.mark.asyncio
async def test_an_ask_user_outside_a_graph_node_is_unchanged() -> None:
    sp, store, bus, router = await _setup(graph=False, pending_gate=GA, correlation_gate=GA)

    outcome, _gate_after = await _reply(router, sp)

    assert [k for k, _ in bus.published] == [FLAT_KEY] and bus.flipped == 1
    assert outcome.kind == "gate" and (await store.get(GS)).parked_status == "resumable"


@pytest.mark.asyncio
async def test_the_gate_is_cleared_once_it_is_answered() -> None:
    """One reply answers one gate: the NEXT reply in the thread steers instead of re-publishing onto a dead key."""
    sp, _store, _bus, router = await _setup(graph=True, pending_gate=GA, correlation_gate=GA)

    _outcome, gate_after = await _reply(router, sp)

    assert gate_after in (None, ""), gate_after
