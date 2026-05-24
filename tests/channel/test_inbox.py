"""ChannelInbox publishes ResponseEnvelopes onto the event bus.

Adaptation note: InMemoryEventBus.subscribe() returns an async iterator
(no key filter, no callback). Tests subscribe before invoking handle_response,
then consume the next event from the iterator and assert event_key + payload.
"""

from __future__ import annotations

import asyncio

import pytest

from matrix.bus.in_memory import InMemoryEventBus
from matrix.channel.adapter import ResponseEnvelope
from matrix.channel.inbox import ChannelInbox
from matrix.model.except_ import BadRequestError


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
