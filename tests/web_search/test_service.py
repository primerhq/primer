"""WebSearchService dispatch + fallback chain + cache TTL tests."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from collections.abc import Callable

import pytest

from primer.model.except_ import NotFoundError
from primer.model.web_search import (
    ACTIVE_WEB_SEARCH_CONFIG_ID,
    ActiveWebSearchConfig,
    AggregatedProviderConfig,
    DuckDuckGoConfig,
    SingleProviderConfig,
    WebSearchProvider,
    WebSearchProviderType,
)
from primer.web_search.adapter import (
    SearchHit,
    WebSearchAdapter,
    WebSearchProviderError,
    WebSearchUnavailable,
)
from primer.web_search.service import WebSearchService


# ---------- Doubles -----------------------------------------------


class _ProgrammableAdapter(WebSearchAdapter):
    """Adapter whose search() returns from a programmed sequence."""

    def __init__(self, name: str, plan: list[Any]) -> None:
        """plan items: list[SearchHit] or BaseException to raise."""
        self.name = name
        self._plan = list(plan)
        self.calls = 0

    async def search(self, *, query, count, safe_search):
        self.calls += 1
        if not self._plan:
            raise RuntimeError(f"{self.name}: out of plan")
        nxt = self._plan.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class _StubRegistry:
    """In-memory registry: maps id -> ProgrammableAdapter (or KeyError)."""

    def __init__(self, adapters: dict[str, _ProgrammableAdapter]) -> None:
        self._adapters = adapters

    async def get(self, provider_id: str) -> WebSearchAdapter:
        if provider_id not in self._adapters:
            raise NotFoundError(f"no such provider {provider_id!r}")
        return self._adapters[provider_id]


class _StubConfigStorage:
    """Storage[ActiveWebSearchConfig] that returns a programmed row."""

    def __init__(self, row: ActiveWebSearchConfig | None) -> None:
        self.row = row
        self.get_calls = 0

    async def get(self, _id: str) -> ActiveWebSearchConfig | None:
        self.get_calls += 1
        return self.row


def _single(provider_id: str) -> ActiveWebSearchConfig:
    return ActiveWebSearchConfig(
        id=ACTIVE_WEB_SEARCH_CONFIG_ID,
        config=SingleProviderConfig(provider_id=provider_id),
    )


def _aggregated(ids: list[str]) -> ActiveWebSearchConfig:
    return ActiveWebSearchConfig(
        id=ACTIVE_WEB_SEARCH_CONFIG_ID,
        config=AggregatedProviderConfig(provider_ids=ids),
    )


def _hits(name: str) -> list[SearchHit]:
    return [SearchHit(title=name, url=f"https://{name}/", snippet="")]


# ---------- Single mode -------------------------------------------


class TestSingleMode:
    @pytest.mark.asyncio
    async def test_single_success_returns_hits(self) -> None:
        a = _ProgrammableAdapter("A", [_hits("A")])
        svc = WebSearchService(
            registry=_StubRegistry({"A": a}),
            active_config_storage=_StubConfigStorage(_single("A")),
        )
        hits = await svc.search(query="q", count=1, safe_search="moderate")
        assert hits == _hits("A")
        assert a.calls == 1

    @pytest.mark.asyncio
    async def test_single_unavailable_propagates(self) -> None:
        a = _ProgrammableAdapter("A", [WebSearchUnavailable("down")])
        svc = WebSearchService(
            registry=_StubRegistry({"A": a}),
            active_config_storage=_StubConfigStorage(_single("A")),
        )
        with pytest.raises(WebSearchUnavailable):
            await svc.search(query="q", count=1, safe_search="moderate")

    @pytest.mark.asyncio
    async def test_single_runtime_error_propagates_unwrapped(self) -> None:
        a = _ProgrammableAdapter("A", [RuntimeError("bug")])
        svc = WebSearchService(
            registry=_StubRegistry({"A": a}),
            active_config_storage=_StubConfigStorage(_single("A")),
        )
        with pytest.raises(RuntimeError):
            await svc.search(query="q", count=1, safe_search="moderate")


# ---------- Aggregated mode ---------------------------------------


class TestAggregatedMode:
    @pytest.mark.asyncio
    async def test_aggregated_first_success_short_circuits(self) -> None:
        a = _ProgrammableAdapter("A", [_hits("A")])
        b = _ProgrammableAdapter("B", [_hits("B")])
        svc = WebSearchService(
            registry=_StubRegistry({"A": a, "B": b}),
            active_config_storage=_StubConfigStorage(_aggregated(["A", "B"])),
        )
        hits = await svc.search(query="q", count=1, safe_search="moderate")
        assert hits == _hits("A")
        assert a.calls == 1
        assert b.calls == 0

    @pytest.mark.asyncio
    async def test_aggregated_falls_back_on_unavailable(self) -> None:
        a = _ProgrammableAdapter("A", [WebSearchUnavailable("down")])
        b = _ProgrammableAdapter("B", [_hits("B")])
        svc = WebSearchService(
            registry=_StubRegistry({"A": a, "B": b}),
            active_config_storage=_StubConfigStorage(_aggregated(["A", "B"])),
        )
        hits = await svc.search(query="q", count=1, safe_search="moderate")
        assert hits == _hits("B")
        assert a.calls == 1
        assert b.calls == 1

    @pytest.mark.asyncio
    async def test_aggregated_falls_back_on_provider_error(self) -> None:
        a = _ProgrammableAdapter("A", [WebSearchProviderError("auth")])
        b = _ProgrammableAdapter("B", [_hits("B")])
        svc = WebSearchService(
            registry=_StubRegistry({"A": a, "B": b}),
            active_config_storage=_StubConfigStorage(_aggregated(["A", "B"])),
        )
        hits = await svc.search(query="q", count=1, safe_search="moderate")
        assert hits == _hits("B")

    @pytest.mark.asyncio
    async def test_aggregated_falls_back_on_not_found_in_registry(self) -> None:
        b = _ProgrammableAdapter("B", [_hits("B")])
        svc = WebSearchService(
            registry=_StubRegistry({"B": b}),
            active_config_storage=_StubConfigStorage(_aggregated(["A", "B"])),
        )
        hits = await svc.search(query="q", count=1, safe_search="moderate")
        assert hits == _hits("B")

    @pytest.mark.asyncio
    async def test_aggregated_runtime_error_propagates(self) -> None:
        a = _ProgrammableAdapter("A", [RuntimeError("bug")])
        b = _ProgrammableAdapter("B", [_hits("B")])
        svc = WebSearchService(
            registry=_StubRegistry({"A": a, "B": b}),
            active_config_storage=_StubConfigStorage(_aggregated(["A", "B"])),
        )
        with pytest.raises(RuntimeError):
            await svc.search(query="q", count=1, safe_search="moderate")
        assert b.calls == 0

    @pytest.mark.asyncio
    async def test_aggregated_all_fail_raises_unavailable_listing_all(self) -> None:
        a = _ProgrammableAdapter("A", [WebSearchUnavailable("a-down")])
        b = _ProgrammableAdapter("B", [WebSearchProviderError("b-auth")])
        svc = WebSearchService(
            registry=_StubRegistry({"A": a, "B": b}),
            active_config_storage=_StubConfigStorage(_aggregated(["A", "B"])),
        )
        with pytest.raises(WebSearchUnavailable) as exc_info:
            await svc.search(query="q", count=1, safe_search="moderate")
        msg = str(exc_info.value)
        assert "all 2 providers failed" in msg
        assert "A:" in msg
        assert "B:" in msg


# ---------- Cache TTL ---------------------------------------------


class TestCacheTtl:
    @pytest.mark.asyncio
    async def test_repeated_search_within_ttl_reads_storage_once(self) -> None:
        a = _ProgrammableAdapter("A", [_hits("A"), _hits("A")])
        storage = _StubConfigStorage(_single("A"))
        svc = WebSearchService(
            registry=_StubRegistry({"A": a}),
            active_config_storage=storage,
            cache_ttl_seconds=10.0,
        )
        await svc.search(query="q", count=1, safe_search="moderate")
        await svc.search(query="q", count=1, safe_search="moderate")
        assert storage.get_calls == 1

    @pytest.mark.asyncio
    async def test_invalidate_forces_reread_on_next_call(self) -> None:
        a = _ProgrammableAdapter("A", [_hits("A"), _hits("A")])
        storage = _StubConfigStorage(_single("A"))
        svc = WebSearchService(
            registry=_StubRegistry({"A": a}),
            active_config_storage=storage,
            cache_ttl_seconds=10.0,
        )
        await svc.search(query="q", count=1, safe_search="moderate")
        svc.invalidate_active_config()
        await svc.search(query="q", count=1, safe_search="moderate")
        assert storage.get_calls == 2

    @pytest.mark.asyncio
    async def test_missing_config_raises_provider_error(self) -> None:
        svc = WebSearchService(
            registry=_StubRegistry({}),
            active_config_storage=_StubConfigStorage(None),
        )
        with pytest.raises(WebSearchProviderError) as exc_info:
            await svc.search(query="q", count=1, safe_search="moderate")
        assert "no active web search config" in str(exc_info.value)
