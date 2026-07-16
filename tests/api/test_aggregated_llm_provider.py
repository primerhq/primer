"""Registry wiring + REST behavior for the aggregated LLM provider."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from primer.api.registries.provider_registry import ProviderRegistry
from primer.llm.aggregated import AggregatedLLM
from primer.model.chat import Done, StreamEvent
from primer.model.except_ import BadRequestError
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

    async def update(self, entity):
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        self._data.pop(id, None)


class _FakeStorageProvider:
    def __init__(self) -> None:
        self._stores: dict[type, _FakeStorage] = {}

    def get_storage(self, model_class: type) -> _FakeStorage:
        return self._stores.setdefault(model_class, _FakeStorage())

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return


def _member_row(pid: str) -> LLMProvider:
    return LLMProvider(
        id=pid,
        provider=LLMProviderType.ANTHROPIC,
        models=[LLMModel(name="claude-x", context_length=200000)],
        config=AnthropicConfig(api_key=SecretStr("sk-x")),
        limits=Limits(max_concurrency=4),
    )


def _agg_row(agg_id: str, members: list[AggregatedMember]) -> LLMProvider:
    return LLMProvider(
        id=agg_id,
        provider=LLMProviderType.AGGREGATED,
        models=[LLMModel(name="virtual-1", context_length=200000)],
        config=AggregatedLLMConfig(members=members),
        limits=Limits(max_concurrency=4),
    )


@pytest.mark.asyncio
async def test_factory_builds_aggregated_llm_via_default_resolver():
    sp = _FakeStorageProvider()
    await sp.get_storage(LLMProvider).create(_member_row("member-1"))
    await sp.get_storage(LLMProvider).create(
        _agg_row("agg-1", [AggregatedMember(provider_id="member-1", model_name="claude-x")])
    )
    # DEFAULT factory (no llm_factory injected) so resolve_member=self.get_llm wired.
    registry = ProviderRegistry(sp)
    adapter = await registry.get_llm("agg-1")
    assert isinstance(adapter, AggregatedLLM)


@pytest.mark.asyncio
async def test_nested_member_raises_bad_request_at_resolve():
    sp = _FakeStorageProvider()
    # agg-2 points at agg-1 (another aggregated provider) -> nesting.
    await sp.get_storage(LLMProvider).create(_member_row("member-1"))
    await sp.get_storage(LLMProvider).create(
        _agg_row("agg-1", [AggregatedMember(provider_id="member-1", model_name="claude-x")])
    )
    await sp.get_storage(LLMProvider).create(
        _agg_row("agg-2", [AggregatedMember(provider_id="agg-1", model_name="virtual-1")])
    )
    registry = ProviderRegistry(sp)
    outer = await registry.get_llm("agg-2")
    with pytest.raises(BadRequestError, match="nesting"):
        [ev async for ev in outer.stream(model="virtual-1", messages=[])]


@pytest.mark.asyncio
async def test_self_reference_raises_bad_request():
    sp = _FakeStorageProvider()
    await sp.get_storage(LLMProvider).create(
        _agg_row("agg-1", [AggregatedMember(provider_id="agg-1", model_name="virtual-1")])
    )
    registry = ProviderRegistry(sp)
    agg = await registry.get_llm("agg-1")
    with pytest.raises(BadRequestError, match="nesting|self-reference"):
        [ev async for ev in agg.stream(model="virtual-1", messages=[])]


@pytest.mark.asyncio
async def test_member_edit_is_picked_up_lazily_per_call():
    # A stub adapter whose stream records a version tag so we can prove the
    # aggregated adapter re-resolves the member after invalidation.
    class _Stub(AggregatedMemberStub):
        pass

    sp = _FakeStorageProvider()
    await sp.get_storage(LLMProvider).create(_member_row("member-1"))
    await sp.get_storage(LLMProvider).create(
        _agg_row("agg-1", [AggregatedMember(provider_id="member-1", model_name="claude-x")])
    )

    versions = {"member-1": 0}

    def factory(row: LLMProvider):
        if row.provider == LLMProviderType.AGGREGATED:
            from primer.llm.aggregated import AggregatedLLM as _Agg
            return _Agg(row, resolve_member=registry.get_llm)
        return AggregatedMemberStub(version=versions[row.id])

    registry = ProviderRegistry(sp, llm_factory=factory)
    agg = await registry.get_llm("agg-1")

    ev1 = [e async for e in agg.stream(model="virtual-1", messages=[])]
    assert ev1[0].raw_reason == "v0"

    versions["member-1"] = 1
    await registry.invalidate_llm("member-1")

    ev2 = [e async for e in agg.stream(model="virtual-1", messages=[])]
    assert ev2[0].raw_reason == "v1"  # re-resolved member picked up the edit


class AggregatedMemberStub:
    """Minimal LLM stub whose Done.raw_reason encodes a version tag."""

    def __init__(self, *, version: int = 0) -> None:
        self._version = version

    async def list_models(self):
        return ["claude-x"]

    async def count_tokens(self, *, model, messages, tools=None) -> int:
        return 1

    async def stream(self, *, model, messages, **kwargs) -> "StreamEvent":
        yield Done(stop_reason="stop", raw_reason=f"v{self._version}")

    async def aclose(self) -> None:
        return
