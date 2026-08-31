"""ChannelInbox publishes ResponseEnvelopes onto the event bus.

Adaptation note: InMemoryEventBus.subscribe() returns an async iterator
(no key filter, no callback). Tests subscribe before invoking handle_response,
then consume the next event from the iterator and assert event_key + payload.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.adapter import ResponseEnvelope
from primer.channel.inbox import ChannelInbox
from primer.model.except_ import BadRequestError
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


@pytest.mark.asyncio
async def test_ask_user_envelope_published_with_correct_key():
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        sub = bus.subscribe()
        try:
            inbox = ChannelInbox(event_bus=bus)
            await inbox.handle_response(
                ResponseEnvelope(
                    kind="ask_user",
                    workspace_id="ws-1",
                    session_id="s-1",
                    tool_call_id="tc-1",
                    response="the answer is 42",
                    decision=None,
                    reason=None,
                ),
            )
            event = await asyncio.wait_for(anext(sub), timeout=1.0)
            assert event.event_key == "ask_user:s-1:tc-1"
            assert event.payload == {"response": "the answer is 42"}
        finally:
            await sub.aclose()
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_tool_approval_envelope_published_with_decision_payload():
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        sub = bus.subscribe()
        try:
            inbox = ChannelInbox(event_bus=bus)
            await inbox.handle_response(
                ResponseEnvelope(
                    kind="tool_approval",
                    workspace_id="ws-1",
                    session_id="s-1",
                    tool_call_id="tc-1",
                    response=None,
                    decision="rejected",
                    reason="not now",
                ),
            )
            event = await asyncio.wait_for(anext(sub), timeout=1.0)
            assert event.event_key == "tool_approval:s-1:tc-1"
            assert event.payload == {"decision": "rejected", "reason": "not now"}
        finally:
            await sub.aclose()
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_unknown_kind_rejected():
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        inbox = ChannelInbox(event_bus=bus)
        with pytest.raises(BadRequestError):
            await inbox.handle_response(
                ResponseEnvelope(
                    kind="not-a-kind",
                    workspace_id="ws", session_id="s", tool_call_id="tc",
                    response=None, decision=None, reason=None,
                ),
            )
    finally:
        await bus.aclose()


# ===========================================================================
# 01a0518f: storage_provider-backed lookup (replaces reconstruction)
# ===========================================================================


def _session(sid: str, *, parked_state: dict) -> WorkspaceSession:
    now = datetime.now(timezone.utc)
    return WorkspaceSession(
        id=sid,
        workspace_id="ws-1",
        binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING,
        created_at=now,
        parked_status="parked",
        parked_at=now,
        parked_state=parked_state,
    )


async def _handle_and_capture(inbox: ChannelInbox, env: ResponseEnvelope, bus):
    sub = bus.subscribe()
    try:
        await inbox.handle_response(env)
        return await asyncio.wait_for(anext(sub), timeout=1.0)
    finally:
        await sub.aclose()


@pytest.mark.asyncio
async def test_lookup_finds_the_stored_key_for_a_plain_single_park():
    """A non-graph park's looked-up key matches what reconstruction would
    have produced anyway - the lookup path is a superset, not a behaviour
    change, for the case that already worked."""
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(_session(
        "s-1",
        parked_state={
            "tool_call_id": "tc-1",
            "yielded": {
                "tool_name": "ask_user",
                "event_key": "ask_user:s-1:tc-1",
            },
        },
    ))
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        inbox = ChannelInbox(event_bus=bus, storage_provider=sp)
        event = await _handle_and_capture(
            inbox,
            ResponseEnvelope(
                kind="ask_user", workspace_id="ws-1", session_id="s-1",
                tool_call_id="tc-1", response="42", decision=None, reason=None,
            ),
            bus,
        )
        assert event.event_key == "ask_user:s-1:tc-1"
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_lookup_finds_a_node_qualified_graph_event_key():
    """01a0518f: THE regression this fix closes. A graph agent-node's
    approval park now stores a node-qualified event_key
    (tool_approval:<session>:<node>:<tcid>) - reconstruction from the
    channel reply alone (which only ever carries session_id + tool_call_id)
    can no longer reproduce it. The lookup must find and use the row's
    OWN stored key instead."""
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    qualified_key = "tool_approval:s-2:worker[0]:call_0"
    await sp.get_storage(WorkspaceSession).create(_session(
        "s-2",
        parked_state={
            "yielded": {"tool_name": "_approval", "event_key": "irrelevant"},
            "graph_checkpoint": {
                "pending_agent_yields": [
                    {
                        "node_id": "worker[0]",
                        "tool_call_id": "call_0",
                        "event_key": qualified_key,
                        "tool_name": "_approval",
                    },
                ],
            },
        },
    ))
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        inbox = ChannelInbox(event_bus=bus, storage_provider=sp)
        event = await _handle_and_capture(
            inbox,
            ResponseEnvelope(
                kind="tool_approval", workspace_id="ws-1", session_id="s-2",
                tool_call_id="call_0", response=None,
                decision="approved", reason=None,
            ),
            bus,
        )
        # NOT the reconstructed "tool_approval:s-2:call_0" - the actual
        # node-qualified key the park stored.
        assert event.event_key == qualified_key
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_lookup_falls_back_when_session_not_found():
    """A storage_provider is wired but env.session_id resolves to nothing
    (e.g. a chat-surface response, or a stale/duplicate webhook) - falls
    back to the reconstructed key exactly like the pre-fix behaviour."""
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        inbox = ChannelInbox(event_bus=bus, storage_provider=sp)
        event = await _handle_and_capture(
            inbox,
            ResponseEnvelope(
                kind="ask_user", workspace_id="ws-1", session_id="chat-1",
                tool_call_id="tc-9", response="hi", decision=None, reason=None,
            ),
            bus,
        )
        assert event.event_key == "ask_user:chat-1:tc-9"
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_lookup_falls_back_when_no_pending_entry_matches():
    """The session exists and is parked, but on a DIFFERENT tool_call_id
    than the reply names - falls back rather than raising or misrouting."""
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(_session(
        "s-3",
        parked_state={
            "tool_call_id": "tc-other",
            "yielded": {
                "tool_name": "ask_user",
                "event_key": "ask_user:s-3:tc-other",
            },
        },
    ))
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        inbox = ChannelInbox(event_bus=bus, storage_provider=sp)
        event = await _handle_and_capture(
            inbox,
            ResponseEnvelope(
                kind="ask_user", workspace_id="ws-1", session_id="s-3",
                tool_call_id="tc-mismatched", response="hi",
                decision=None, reason=None,
            ),
            bus,
        )
        assert event.event_key == "ask_user:s-3:tc-mismatched"
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_lookup_warns_and_resolves_the_first_on_a_genuine_collision(
    caplog,
):
    """01a0518f: two fan-out siblings sharing a raw provider tool_call_id
    still can't be told apart from a channel reply alone (the wire format
    has no other field to disambiguate - same ambiguity the REST respond
    routes already accept). Must not raise or drop the reply - resolves
    the first match and logs a warning so the collision is visible."""
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(_session(
        "s-4",
        parked_state={
            "yielded": {"tool_name": "_approval", "event_key": "irrelevant"},
            "graph_checkpoint": {
                "pending_agent_yields": [
                    {
                        "node_id": "worker[0]", "tool_call_id": "call_0",
                        "event_key": "tool_approval:s-4:worker[0]:call_0",
                        "tool_name": "_approval",
                    },
                    {
                        "node_id": "worker[1]", "tool_call_id": "call_0",
                        "event_key": "tool_approval:s-4:worker[1]:call_0",
                        "tool_name": "_approval",
                    },
                ],
            },
        },
    ))
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        inbox = ChannelInbox(event_bus=bus, storage_provider=sp)
        with caplog.at_level(logging.WARNING, logger="primer.channel.inbox"):
            event = await _handle_and_capture(
                inbox,
                ResponseEnvelope(
                    kind="tool_approval", workspace_id="ws-1",
                    session_id="s-4", tool_call_id="call_0", response=None,
                    decision="approved", reason=None,
                ),
                bus,
            )
        assert event.event_key in (
            "tool_approval:s-4:worker[0]:call_0",
            "tool_approval:s-4:worker[1]:call_0",
        )
        assert any(
            "2 pending entries match" in r.message for r in caplog.records
        )
    finally:
        await bus.aclose()
