"""ChannelDispatcher fans out to all enabled channels for a workspace."""

from __future__ import annotations

import asyncio

import pytest

from matrix.channel.adapter import PromptEnvelope
from matrix.channel.dispatcher import ChannelDispatcher
from matrix.channel.null_adapter import NullChannelAdapter


class _StubRegistry:
    def __init__(self, pairs: list[tuple[NullChannelAdapter, dict]]) -> None:
        self._pairs = pairs

    async def for_workspace(self, workspace_id: str):
        return self._pairs


def _env(*, kind: str = "ask_user") -> PromptEnvelope:
    return PromptEnvelope(
        kind=kind,
        workspace_id="ws-1",
        session_id="s-1",
        tool_call_id="tc-1",
        prompt="please answer",
        response_schema=None,
        choices=None,
        timeout_at_iso=None,
    )


@pytest.mark.asyncio
async def test_dispatch_fans_out_to_all_enabled():
    a, b = NullChannelAdapter(), NullChannelAdapter()
    await a.initialize(); await b.initialize()
    registry = _StubRegistry([
        (a, {"forward_ask_user": True}),
        (b, {"forward_ask_user": True}),
    ])
    d = ChannelDispatcher(registry=registry)
    await d.dispatch_prompt(envelope=_env())
    assert len(a.posted) == 1
    assert len(b.posted) == 1


@pytest.mark.asyncio
async def test_dispatch_respects_per_envelope_forward_flag():
    a, b = NullChannelAdapter(), NullChannelAdapter()
    await a.initialize(); await b.initialize()
    registry = _StubRegistry([
        (a, {"forward_ask_user": False, "forward_tool_approval": True}),
        (b, {"forward_ask_user": True,  "forward_tool_approval": True}),
    ])
    d = ChannelDispatcher(registry=registry)
    await d.dispatch_prompt(envelope=_env(kind="ask_user"))
    assert a.posted == []
    assert len(b.posted) == 1


@pytest.mark.asyncio
async def test_dispatch_one_adapter_failure_does_not_block_others():
    class _Bad(NullChannelAdapter):
        async def post_prompt(self, envelope):
            raise RuntimeError("network down")
    bad, good = _Bad(), NullChannelAdapter()
    await bad.initialize(); await good.initialize()
    registry = _StubRegistry([
        (bad, {"forward_ask_user": True}),
        (good, {"forward_ask_user": True}),
    ])
    d = ChannelDispatcher(registry=registry)
    await d.dispatch_prompt(envelope=_env())
    assert len(good.posted) == 1
