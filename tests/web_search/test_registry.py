"""WebSearchRegistry tests — mirrors SemanticSearchRegistry's race-
resilience pattern: per-id lazy cache, concurrent-get safety,
invalidate via aclose(), full teardown via registry.aclose()."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from primer.model.except_ import NotFoundError
from primer.model.web_search import (
    DuckDuckGoConfig,
    WebSearchProvider,
    WebSearchProviderType,
)
from primer.web_search.adapter import SearchHit, WebSearchAdapter

from primer.api.registries.web_search_registry import (
    WebSearchRegistry,
    default_web_search_factory,
)


# ---------- Doubles -----------------------------------------------


class _StubAdapter(WebSearchAdapter):
    """In-memory WebSearchAdapter that records calls."""

    def __init__(self, row: WebSearchProvider) -> None:
        self.row = row
        self.aclose_count = 0
        self.search_count = 0

    async def search(self, *, query, count, safe_search):
        self.search_count += 1
        return [SearchHit(title=self.row.id, url="https://x/", snippet="")]

    async def aclose(self) -> None:
        self.aclose_count += 1


class _StubStorage:
    """In-memory Storage[WebSearchProvider] just enough for the
    registry's get() path."""

    def __init__(self, rows: dict[str, WebSearchProvider]) -> None:
        self._rows = rows

    async def get(self, id: str) -> WebSearchProvider | None:
        return self._rows.get(id)


def _row(pid: str) -> WebSearchProvider:
    return WebSearchProvider(
        id=pid,
        provider_type=WebSearchProviderType.DUCKDUCKGO,
        config=DuckDuckGoConfig(),
    )


def _stub_factory_with_log(log: list[_StubAdapter]):
    def factory(row: WebSearchProvider) -> WebSearchAdapter:
        inst = _StubAdapter(row)
        log.append(inst)
        return inst

    return factory


# ---------- Tests -------------------------------------------------


class TestGetAndCache:
    @pytest.mark.asyncio
    async def test_get_caches_per_id(self) -> None:
        log: list[_StubAdapter] = []
        reg = WebSearchRegistry(
            storage=_StubStorage({"A": _row("A")}),
            factory=_stub_factory_with_log(log),
        )
        first = await reg.get("A")
        second = await reg.get("A")
        assert first is second
        assert len(log) == 1

    @pytest.mark.asyncio
    async def test_get_missing_id_raises_not_found(self) -> None:
        reg = WebSearchRegistry(
            storage=_StubStorage({}),
            factory=_stub_factory_with_log([]),
        )
        with pytest.raises(NotFoundError):
            await reg.get("missing")

    @pytest.mark.asyncio
    async def test_concurrent_get_same_id_yields_single_cached_instance(self) -> None:
        log: list[_StubAdapter] = []
        reg = WebSearchRegistry(
            storage=_StubStorage({"A": _row("A")}),
            factory=_stub_factory_with_log(log),
        )
        a, b = await asyncio.gather(reg.get("A"), reg.get("A"))
        assert a is b
        winners = [inst for inst in log if inst.aclose_count == 0]
        assert len(winners) == 1

    @pytest.mark.asyncio
    async def test_concurrent_get_different_ids_does_not_serialise(self) -> None:
        log: list[_StubAdapter] = []
        reg = WebSearchRegistry(
            storage=_StubStorage({"A": _row("A"), "B": _row("B")}),
            factory=_stub_factory_with_log(log),
        )
        a, b = await asyncio.gather(reg.get("A"), reg.get("B"))
        assert a is not b
        assert a.row.id == "A"
        assert b.row.id == "B"


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_drops_and_acloses_cached_instance(self) -> None:
        log: list[_StubAdapter] = []
        reg = WebSearchRegistry(
            storage=_StubStorage({"A": _row("A")}),
            factory=_stub_factory_with_log(log),
        )
        first = await reg.get("A")
        await reg.invalidate("A")
        assert first.aclose_count == 1
        second = await reg.get("A")
        assert second is not first
        assert len(log) == 2

    @pytest.mark.asyncio
    async def test_invalidate_unknown_id_is_no_op(self) -> None:
        reg = WebSearchRegistry(
            storage=_StubStorage({}),
            factory=_stub_factory_with_log([]),
        )
        await reg.invalidate("never-cached")


class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_drops_and_acloses_all_cached(self) -> None:
        log: list[_StubAdapter] = []
        reg = WebSearchRegistry(
            storage=_StubStorage({"A": _row("A"), "B": _row("B")}),
            factory=_stub_factory_with_log(log),
        )
        await reg.get("A")
        await reg.get("B")
        assert all(inst.aclose_count == 0 for inst in log if inst.row.id in {"A", "B"})
        await reg.aclose()
        winners_by_id = {inst.row.id: inst for inst in log if inst.aclose_count == 1}
        assert set(winners_by_id) == {"A", "B"}


class TestDefaultFactory:
    def test_factory_constructs_ddg_adapter(self) -> None:
        from primer.web_search.duckduckgo import DuckDuckGoAdapter

        row = WebSearchProvider(
            id="DuckDuckGo",
            provider_type=WebSearchProviderType.DUCKDUCKGO,
            config=DuckDuckGoConfig(),
        )
        adapter = default_web_search_factory(row)
        assert isinstance(adapter, DuckDuckGoAdapter)

    def test_factory_constructs_tavily_adapter(self) -> None:
        from pydantic import SecretStr

        from primer.model.web_search import TavilyConfig
        from primer.web_search.tavily import TavilyAdapter

        row = WebSearchProvider(
            id="tavily-prod",
            provider_type=WebSearchProviderType.TAVILY,
            config=TavilyConfig(api_key=SecretStr("tvly-x")),
        )
        adapter = default_web_search_factory(row)
        assert isinstance(adapter, TavilyAdapter)
