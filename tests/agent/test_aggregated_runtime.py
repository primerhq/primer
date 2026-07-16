"""_resolve_agent_runtime resolves an aggregated provider + virtual model."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from primer.agent.invoke import _resolve_agent_runtime
from primer.api.registries.provider_registry import ProviderRegistry
from primer.llm.aggregated import AggregatedLLM
from primer.model.agent import Agent, AgentModel
from primer.model.provider import (
    AggregatedLLMConfig,
    AggregatedMember,
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


class _FakeStorage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str):
        return self._data.get(id)

    async def create(self, entity):
        self._data[entity.id] = entity
        return entity


class _FakeStorageProvider:
    def __init__(self) -> None:
        self._stores: dict[type, _FakeStorage] = {}

    def get_storage(self, model_class: type) -> _FakeStorage:
        return self._stores.setdefault(model_class, _FakeStorage())


@pytest.mark.asyncio
async def test_resolves_aggregated_runtime_and_virtual_model():
    sp = _FakeStorageProvider()
    await sp.get_storage(LLMProvider).create(
        LLMProvider(
            id="member-1", provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="claude-x", context_length=200000)],
            config=AnthropicConfig(api_key=SecretStr("sk-x")),
            limits=Limits(max_concurrency=4),
        )
    )
    await sp.get_storage(LLMProvider).create(
        LLMProvider(
            id="agg-1", provider=LLMProviderType.AGGREGATED,
            models=[LLMModel(name="virtual-1", context_length=200000)],
            config=AggregatedLLMConfig(members=[
                AggregatedMember(provider_id="member-1", model_name="claude-x"),
            ]),
            limits=Limits(max_concurrency=4),
        )
    )
    await sp.get_storage(Agent).create(
        Agent(
            id="ag-1", description="test agent",
            model=AgentModel(provider_id="agg-1", model_name="virtual-1"),
        )
    )
    registry = ProviderRegistry(sp)

    agent, llm, llm_model = await _resolve_agent_runtime(
        "ag-1", storage_provider=sp, provider_registry=registry,
    )
    assert isinstance(llm, AggregatedLLM)
    assert llm_model.name == "virtual-1"   # virtual name matched on the agg row
