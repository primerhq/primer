"""Tests for the ``invoke_agent`` system tool.

A non-yielding tool that runs another agent once on a prompt (via
``run_subagent``) and returns its text as ``{output: <text>}``. The
``run_subagent`` call is monkeypatched at ``primer.toolset.system``
so these tests exercise only the handler's wiring + error mapping.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from primer.api.registries import ProviderRegistry
from primer.toolset.system import build_system_toolset


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get_storage(self, cls: type) -> "_Storage":  # pragma: no cover - stub
        return self


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls: type) -> _Storage:
        return self._stores.setdefault(cls, _Storage())

    async def initialize(self) -> None:  # pragma: no cover - stub
        return

    async def aclose(self) -> None:  # pragma: no cover - stub
        return


@pytest.fixture
def system_provider():
    sp = _SP()
    pr = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    return build_system_toolset(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
    )


@pytest.mark.asyncio
async def test_invoke_agent_returns_subagent_text(monkeypatch, system_provider):
    async def _fake_run_subagent(**kwargs):
        assert kwargs["agent_id"] == "agent-B"
        assert kwargs["prompt"] == "summarise X"
        return "the summary"

    monkeypatch.setattr(
        "primer.toolset.system.run_subagent", _fake_run_subagent
    )
    res = await system_provider.call(
        tool_name="invoke_agent",
        arguments={"agent_id": "agent-B", "prompt": "summarise X"},
        principal=None,
        ctx=None,
    )
    assert res.is_error is False
    assert json.loads(res.output) == {"output": "the summary"}


@pytest.mark.asyncio
async def test_invoke_agent_depth_exceeded_is_error(monkeypatch, system_provider):
    from primer.agent.invoke import InvocationDepthExceeded

    async def _boom(**kwargs):
        raise InvocationDepthExceeded("too deep")

    monkeypatch.setattr("primer.toolset.system.run_subagent", _boom)
    res = await system_provider.call(
        tool_name="invoke_agent",
        arguments={"agent_id": "a", "prompt": "p"},
        principal=None,
        ctx=None,
    )
    assert res.is_error is True
    assert "depth" in res.output.lower()
