"""ChannelDispatcher fans out to all channel adapters for a workspace."""

from __future__ import annotations

import pytest

from primer.channel.adapter import PromptEnvelope
from primer.channel.dispatcher import ChannelDispatcher
from primer.channel.null_adapter import NullChannelAdapter


class _StubRegistry:
    def __init__(self, adapters: list[NullChannelAdapter]) -> None:
        self._adapters = adapters

    async def for_workspace(self, workspace_id: str):
        return self._adapters


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
async def test_dispatch_fans_out_to_all_adapters():
    a, b = NullChannelAdapter(), NullChannelAdapter()
    await a.initialize(); await b.initialize()
    registry = _StubRegistry([a, b])
    d = ChannelDispatcher(registry=registry)
    await d.dispatch_prompt(envelope=_env())
    assert len(a.posted) == 1
    assert len(b.posted) == 1


@pytest.mark.asyncio
async def test_dispatch_no_adapters_returns_empty():
    registry = _StubRegistry([])
    d = ChannelDispatcher(registry=registry)
    result = await d.dispatch_prompt(envelope=_env())
    assert result == []


@pytest.mark.asyncio
async def test_dispatch_one_adapter_failure_does_not_block_others():
    class _Bad(NullChannelAdapter):
        async def post_prompt(self, envelope):
            raise RuntimeError("network down")
    bad, good = _Bad(), NullChannelAdapter()
    await bad.initialize(); await good.initialize()
    registry = _StubRegistry([bad, good])
    d = ChannelDispatcher(registry=registry)
    results = await d.dispatch_prompt(envelope=_env())
    assert len(good.posted) == 1
    # error dict from bad adapter is included in results
    assert any("error" in r for r in results)
