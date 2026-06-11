"""Chat-surface approval gate: a gated tool call must park via YieldToWorker
with a chat-scoped event key.

Mirrors tests/agent/test_tool_manager_approval_gate.py but exercises the
chat path (chat_id set, no workspace_session).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.agent.approval import ApprovalResolver
from primer.agent.tool_manager import ToolExecutionManager
from primer.chat.dispatch import ChatDispatchDeps
from primer.model.chat import Tool, ToolCallPart, ToolCallResult
from primer.model.tool_approval import (
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from primer.model.yield_ import YieldToWorker


class _EchoProvider:
    """Minimal fake ToolsetProvider for toolset_id '_test', tool 'echo'."""

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(
            id="echo",
            description="echoes its arguments",
            toolset_id="_test",
            args_schema={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "additionalProperties": False,
            },
        )

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx=None,
    ) -> ToolCallResult:
        return ToolCallResult(output=str(arguments), is_error=False)


class _PoliciesOnlyResolver(ApprovalResolver):
    """Test resolver bypassing storage."""

    def __init__(self, policies: list[ToolApprovalPolicy]) -> None:
        self._policies = policies
        self._ttl = 60.0
        self._cache = {}

    async def find(self, *, toolset_id, tool_name):
        for p in self._policies:
            if p.toolset_id == toolset_id and p.tool_name == tool_name:
                return p
        return None


# The catalog scopes tool names as ``toolset_id__bare_name``.
_SCOPED_NAME = "_test__echo"


@pytest.mark.asyncio
async def test_chat_gated_tool_yields_for_approval(
):
    tm = ToolExecutionManager(
        toolset_providers={"_test": _EchoProvider()},  # type: ignore[arg-type]
        chat_id="chat-1",
    )
    tm._approval_resolver = _PoliciesOnlyResolver(
        [
            ToolApprovalPolicy(
                id="p",
                toolset_id="_test",
                tool_name="echo",
                approval=RequiredApprovalConfig(),
            ),
        ]
    )

    gated_call = ToolCallPart(id="c1", name=_SCOPED_NAME, arguments={"x": 1})
    with pytest.raises(YieldToWorker) as ei:
        await tm.execute(gated_call)

    y = ei.value.yielded
    assert y.tool_name == "_approval"
    assert y.event_key == "tool_approval:chat-1:" + gated_call.id
    assert y.resume_metadata["original_call"]["name"] == gated_call.name
    assert y.resume_metadata["original_call"]["arguments"] == {"x": 1}


@pytest.mark.asyncio
async def test_build_runner_wires_approval_resolver(
    fake_storage_provider, fake_provider_registry,
):
    """_build_runner must construct + wire an ApprovalResolver into the
    chat ToolExecutionManager so the approval gate is live for chats."""
    import asyncio
    from datetime import datetime, timezone

    from pydantic import SecretStr

    from primer.chat.dispatch import _build_runner
    from primer.model.agent import Agent, AgentModel
    from primer.model.chats import Chat
    from primer.model.provider import (
        AnthropicConfig, Limits, LLMModel, LLMProvider, LLMProviderType,
    )

    await fake_storage_provider.get_storage(LLMProvider).create(
        LLMProvider(
            id="p", provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="m", context_length=8192)],
            config=AnthropicConfig(api_key=SecretStr("test")),
            limits=Limits(max_concurrency=1),
        ),
    )
    await fake_storage_provider.get_storage(Agent).create(Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    ))
    chat = Chat(
        id="chat-1", agent_id="ag", title="t",
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(Chat).create(chat)

    fake_llm = object()

    deps = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        event_bus=object(),  # type: ignore[arg-type]
        chat_tick_router=object(),  # type: ignore[arg-type]
        fake_llm=fake_llm,  # type: ignore[arg-type]
    )

    runner = await _build_runner(deps, chat, asyncio.Event())
    assert runner is not None
    assert runner._tools._approval_resolver is not None
    # Chat-surface inform delivery is deferred (channels-drive-chats), so no
    # inform sink is wired on the chat runner: inform_user returns delivered_to:0.
    assert runner._tools._inform_sink is None
