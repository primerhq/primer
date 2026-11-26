"""GX-1: an evicted adapter is closed only after the calls using it have finished and it has been idle for a while; new requests get a fresh one."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from primer.api.registries.semantic_search_registry import SemanticSearchRegistry
from primer.api.registries.speech_registry import SpeechRegistry
from primer.api.registries.web_fetch_registry import WebFetchRegistry
from primer.api.registries.web_search_registry import WebSearchRegistry

IDLE = 0.15


class _Adapter:
    """Streams in two halves, with a gate between them; records its close."""

    def __init__(self, row=None) -> None:
        self.row = row
        self.closed = 0
        self.gate = asyncio.Event()

    async def initialize(self) -> None:
        return None

    async def stream(self):
        yield "hello "
        await self.gate.wait()
        if self.closed:
            raise RuntimeError("the client was closed under the stream")
        yield "world"

    async def call(self) -> str:
        await self.gate.wait()
        return "done"

    def sync_value(self) -> int:
        return 7

    async def aclose(self) -> None:
        self.closed += 1


async def _settle(seconds: float = IDLE * 4) -> None:
    await asyncio.sleep(seconds)


def _registries(db):
    def make(cls, getter, **kw):
        return cls(storage=db, factory=_Adapter, retire_idle_seconds=IDLE, **kw), getter

    return {
        "semantic": make(SemanticSearchRegistry, "get_provider"),
        "web_search": make(WebSearchRegistry, "get"),
        "web_fetch": make(WebFetchRegistry, "get"),
        "speech": make(SpeechRegistry, "get", label="x"),
    }


class _Rows:
    def __init__(self) -> None:
        self.rows = {"p1": SimpleNamespace(id="p1")}

    async def get(self, _id):
        return self.rows.get(_id)


@pytest.mark.parametrize("which", ["semantic", "web_search", "web_fetch", "speech"])
async def test_an_invalidate_does_not_close_an_adapter_with_a_stream_in_flight(which):
    registry, getter = _registries(_Rows())[which]
    old = await getattr(registry, getter)("p1")
    agen = old.stream()
    assert await agen.__anext__() == "hello "          # the turn is mid-stream
    await registry.invalidate("p1")
    new = await getattr(registry, getter)("p1")
    assert new is not old                              # new requests get a fresh adapter at once
    await _settle()
    assert old.closed == 0                             # but the stream's adapter is still open
    old.gate.set()
    assert [x async for x in agen] == ["world"]        # and the stream finishes
    await _settle()
    assert old.closed == 1                             # closed once, after it went idle
    await registry.aclose()
    assert new.closed == 1


async def test_an_in_flight_call_keeps_the_adapter_open_too():
    registry, getter = _registries(_Rows())["web_search"]
    old = await registry.get("p1")
    call = asyncio.ensure_future(old.call())
    await asyncio.sleep(0)
    await registry.invalidate("p1")
    await _settle()
    assert old.closed == 0
    old.gate.set()
    assert await call == "done"
    await _settle()
    assert old.closed == 1


async def test_an_idle_adapter_is_closed_after_the_idle_period_not_at_once():
    registry, _ = _registries(_Rows())["speech"]
    old = await registry.get("p1")
    await registry.invalidate("p1")
    assert old.closed == 0                              # a turn may still hold it between two calls
    await _settle()
    assert old.closed == 1


async def test_registry_aclose_closes_retired_adapters_at_once():
    registry, _ = _registries(_Rows())["web_fetch"]
    old = await registry.get("p1")
    agen = old.stream()
    await agen.__anext__()
    await registry.invalidate("p1")
    await registry.aclose()
    assert old.closed == 1


async def test_tracking_keeps_identity_and_sync_methods():
    registry, _ = _registries(_Rows())["web_search"]
    a = await registry.get("p1")
    assert a is await registry.get("p1") and a.sync_value() == 7 and isinstance(a, _Adapter)


async def test_an_adapter_that_cannot_be_instrumented_is_still_closed_when_retired():
    class _Slotted:
        __slots__ = ("closed",)

        def __init__(self, row=None) -> None:
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    registry = WebSearchRegistry(storage=_Rows(), factory=_Slotted, retire_idle_seconds=IDLE)
    old = await registry.get("p1")
    await registry.invalidate("p1")
    await _settle()
    assert old.closed == 1


# --- ProviderRegistry (LLM, embedder, cross-encoder, aggregated) ------------------------------------------------------------------

async def test_an_llm_provider_invalidate_leaves_a_stream_in_flight_alone(tmp_path):
    from pydantic import HttpUrl

    from primer.api.registries.provider_registry import ProviderRegistry
    from primer.model.model_profile import ModelProfileConfig
    from primer.model.provider import Limits, LLMProvider, LLMProviderType, OpenChatConfig, OpenChatFlavor
    from primer.model_profile import ResolvedModel
    from primer.storage.sqlite import SqliteConfig, SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "p.sqlite")))
    await sp.initialize()
    try:
        await sp.get_storage(LLMProvider).create(LLMProvider(
            id="p1", provider=LLMProviderType.OPENCHAT,
            models=[ResolvedModel(profile_id="x", provider_id="p1", model_name="m", context_length=8192, config=ModelProfileConfig())],
            config=OpenChatConfig(url=HttpUrl("http://127.0.0.1:9/v1/"), api_key=None, flavor=OpenChatFlavor.OTHER),
            limits=Limits(max_concurrency=4),
        ))
        registry = ProviderRegistry(sp, llm_factory=lambda row: _Adapter(row), retire_idle_seconds=IDLE)
        old = await registry.get_llm("p1")
        agen = old.stream()
        await agen.__anext__()
        await registry.invalidate_llm("p1")
        assert await registry.get_llm("p1") is not old
        await _settle()
        assert old.closed == 0
        old.gate.set()
        assert [x async for x in agen] == ["world"]
        await _settle()
        assert old.closed == 1
        await registry.aclose()
    finally:
        await sp.aclose()


# --- RetryingLLM: the factory shape production hands out (the factory always returns a RetryingLLM) ---


class _SlowInner:
    """A slow async-generator adapter: the reviewer's shape for the 2.1 repro."""

    def __init__(self) -> None:
        self.closed = 0
        self.gate = asyncio.Event()

    async def stream(self, **kwargs):
        yield "a "
        await self.gate.wait()
        if self.closed:
            raise RuntimeError("client closed under the stream")
        yield "b"

    async def aclose(self) -> None:
        self.closed += 1


async def _retrying_registry(tmp_path, *, retries: int = 0):
    from pydantic import HttpUrl

    from primer.api.registries.provider_registry import ProviderRegistry
    from primer.llm.retrying import RetryingLLM
    from primer.model.model_profile import ModelProfileConfig
    from primer.model.provider import Limits, LLMProvider, LLMProviderType, OpenChatConfig, OpenChatFlavor
    from primer.model_profile import ResolvedModel
    from primer.storage.sqlite import SqliteConfig, SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "p.sqlite")))
    await sp.initialize()
    await sp.get_storage(LLMProvider).create(LLMProvider(
        id="p1", provider=LLMProviderType.OPENCHAT,
        models=[ResolvedModel(profile_id="x", provider_id="p1", model_name="m", context_length=8192, config=ModelProfileConfig())],
        config=OpenChatConfig(url=HttpUrl("http://127.0.0.1:9/v1/"), api_key=None, flavor=OpenChatFlavor.OTHER),
        limits=Limits(max_concurrency=4, max_retries=retries),
    ))
    registry = ProviderRegistry(
        sp, llm_factory=lambda row: RetryingLLM(_SlowInner(), row), retire_idle_seconds=IDLE,
    )
    return sp, registry


@pytest.mark.parametrize("retries", [0, 2])
async def test_a_retire_mid_stream_on_a_retrying_llm_does_not_cut_the_stream(tmp_path, retries):
    sp, registry = await _retrying_registry(tmp_path, retries=retries)
    try:
        old = await registry.get_llm("p1")
        inner = old.inner
        agen = old.stream(model="m", messages=[])
        assert await agen.__anext__() == "a "            # a turn is mid-stream
        await registry.invalidate_llm("p1")
        new = await registry.get_llm("p1")
        assert new is not old and new.inner is not inner  # new callers get a fresh adapter at once
        await _settle()
        assert inner.closed == 0                          # but the stream's adapter is still open
        inner.gate.set()
        assert [x async for x in agen] == ["b"]           # and the stream finishes
        await _settle()
        assert inner.closed == 1                           # closed once, after it went idle
        await registry.aclose()
    finally:
        await sp.aclose()


async def _wait_until(predicate, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_a_stuck_stream_is_closed_after_the_close_bound(caplog):
    import logging

    from primer.api.registries.retirement import AdapterRetirement

    inner = _SlowInner()  # the stream parks at its gate forever
    retirement = AdapterRetirement(idle_seconds=0.05, close_bound_seconds=0.2)
    adapter = retirement.track(inner)
    agen = adapter.stream()
    await agen.__anext__()  # mid-stream, and stuck
    retirement.retire(adapter)
    assert inner.closed == 0
    with caplog.at_level(logging.WARNING, logger="primer.api.registries.retirement"):
        assert await _wait_until(lambda: inner.closed >= 1, 3.0)
    assert "without activity" in caplog.text
    await agen.aclose()


class _Chatty:
    """Yields an item every ``period`` seconds, ``count`` times."""

    def __init__(self, period: float, count: int) -> None:
        self.closed = 0
        self.period = period
        self.count = count

    async def stream(self):
        for i in range(self.count):
            await asyncio.sleep(self.period)
            yield i

    async def aclose(self) -> None:
        self.closed += 1


async def test_a_stream_still_producing_output_runs_past_the_close_bound():
    """The bound counts silence, not total time: a long healthy generation must never be cut mid-stream."""
    from primer.api.registries.retirement import AdapterRetirement

    inner = _Chatty(period=0.05, count=16)  # 0.8 s of steady output
    retirement = AdapterRetirement(idle_seconds=0.02, close_bound_seconds=0.2)
    adapter = retirement.track(inner)
    agen = adapter.stream()
    assert await agen.__anext__() == 0
    retirement.retire(adapter)
    seen = [x async for x in agen]  # 4x the bound in total, never 0.2 s without an item
    assert seen == list(range(1, 16))
    assert inner.closed == 0  # not closed under the running stream
    assert await _wait_until(lambda: inner.closed == 1, 3.0)


async def test_a_stream_silent_longer_than_the_bound_is_closed(caplog):
    import logging

    from primer.api.registries.retirement import AdapterRetirement

    inner = _Chatty(period=5.0, count=2)  # one item, then 5 s of silence
    retirement = AdapterRetirement(idle_seconds=0.02, close_bound_seconds=0.2)
    adapter = retirement.track(inner)
    agen = adapter.stream()
    task = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)
    retirement.retire(adapter)
    with caplog.at_level(logging.WARNING, logger="primer.api.registries.retirement"):
        assert await _wait_until(lambda: inner.closed >= 1, 3.0)
    assert "without activity" in caplog.text
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
