"""Tests for the NullChannelAdapter used in unit / integration tests."""

from __future__ import annotations

import pytest

from matrix.channel.adapter import (
    ChannelAdapter,
    PromptEnvelope,
    ResponseEnvelope,
)
from matrix.channel.null_adapter import NullChannelAdapter


def _envelope() -> PromptEnvelope:
    return PromptEnvelope(
        kind="ask_user",
        workspace_id="ws-1",
        session_id="s-1",
        tool_call_id="tc-1",
        prompt="hello",
        response_schema=None,
        choices=None,
        timeout_at_iso=None,
    )


@pytest.mark.asyncio
async def test_null_adapter_records_posts():
    a = NullChannelAdapter()
    await a.initialize()
    try:
        await a.post_prompt(_envelope())
        await a.post_prompt(_envelope())
        assert len(a.posted) == 2
        assert a.posted[0].kind == "ask_user"
    finally:
        await a.aclose()


@pytest.mark.asyncio
async def test_null_adapter_verify_ok():
    a = NullChannelAdapter()
    await a.initialize()
    try:
        await a.verify()
    finally:
        await a.aclose()


def test_null_adapter_is_subclass():
    assert issubclass(NullChannelAdapter, ChannelAdapter)


def test_response_envelope_carries_kind_and_decision():
    r = ResponseEnvelope(
        kind="tool_approval",
        workspace_id="ws-1",
        session_id="s-1",
        tool_call_id="tc-1",
        response=None,
        decision="approved",
        reason=None,
        platform_metadata={"msg_id": "M1"},
    )
    assert r.decision == "approved"
    assert r.platform_metadata == {"msg_id": "M1"}
