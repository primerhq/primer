"""_resolve_agent_runtime resolves an agent bound to an aggregated profile.

Rewritten for the ModelProfile move (01a067c4): there is no more
"aggregated LLMProvider + a profile naming a virtual model on it" pair --
the aggregated ModelProfile IS what an Agent's model.profile_id names
directly, and it carries no model_name of its own (see ResolvedModel's
docstring on why: no single member to attribute a name to).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from primer.agent.invoke import _resolve_agent_runtime
from primer.api.registries.provider_registry import ProviderRegistry
from primer.llm.aggregated import AggregatedLLM
from primer.model.agent import Agent, AgentModel
from primer.model.model_profile import ModelProfile
from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType


class _FakeStorage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str):
        return self._data.get(id)

    async def create(self, entity):
        self._data[entity.id] = entity
        return entity


class _FakeStorageProvider:
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

    def __init__(self) -> None:
        self._stores: dict[type, _FakeStorage] = {}

    def get_storage(self, model_class: type) -> _FakeStorage:
        return self._stores.setdefault(model_class, _FakeStorage())


@pytest.mark.asyncio
async def test_resolves_aggregated_runtime():
    sp = _FakeStorageProvider()
    await sp.get_storage(LLMProvider).create(
        LLMProvider(
            id="member-provider-1",
            provider=LLMProviderType.ANTHROPIC,
            config=AnthropicConfig(api_key=SecretStr("sk-x")),
            limits=Limits(max_concurrency=4),
        )
    )
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="member-1", description="member one",
            provider_id="member-provider-1", model_name="claude-x",
            context_length=200_000,
        )
    )
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="member-2", description="member two",
            provider_id="member-provider-1", model_name="claude-y",
            context_length=8_192,
        )
    )
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="agg-1", description="an aggregated profile",
            kind="aggregated", members=["member-1", "member-2"],
        )
    )
    await sp.get_storage(Agent).create(
        Agent(
            id="ag-1",
            description="test agent",
            model=AgentModel(profile_id="agg-1"),
        )
    )
    registry = ProviderRegistry(sp)

    agent, llm, llm_model = await _resolve_agent_runtime(
        "ag-1",
        storage_provider=sp,
        provider_registry=registry,
    )
    assert isinstance(llm, AggregatedLLM)
    # No single provider/model to report -- see ResolvedModel's docstring
    # (ruling 5's "no fabricated label" flat view).
    assert llm_model.provider_id is None
    assert llm_model.model_name is None
    # MIN over members, so a caller never overpromises the window.
    assert llm_model.context_length == 8_192
