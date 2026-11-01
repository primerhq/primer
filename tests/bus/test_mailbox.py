"""The bounded mailbox behind a bus subscription: its close and its loss signal (perf plan Task 6, review fixes)."""
from __future__ import annotations

import asyncio

import pytest

from primer.bus._mailbox import Mailbox
from primer.bus.in_memory import InMemoryEventBus
from primer.coordinator.postgres import PostgresInvalidationBus
from primer.int.coordinator import InvalidationTopic

pytestmark = pytest.mark.asyncio


# -- close: an offer after close never evicts the sentinel ------------------------------------------------------------------------------------


async def test_a_coalescing_mailbox_ends_after_close_whatever_is_offered_next():
    box = Mailbox(coalesce=True)
    box.offer("a")
    box.close()
    box.offer("b")  # must not evict "a" or the close sentinel
    assert await box.next() == "a"
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(box.next(), 1)


async def test_a_full_mailbox_ends_after_close_however_many_events_follow():
    box = Mailbox(maxsize=1000)
    for i in range(1000):
        box.offer(i)
    box.close()
    for i in range(1001):
        box.offer(f"late-{i}")
    got = []
    with pytest.raises(StopAsyncIteration):
        while True:
            got.append(await asyncio.wait_for(box.next(), 1))
    assert got == list(range(1000)), "what was queued before the close is delivered, nothing after it is"
    assert box.qsize() == 0


async def test_close_is_idempotent_and_the_sentinel_is_never_counted_as_dropped():
    box = Mailbox(maxsize=2)
    box.offer(1)
    box.close()
    box.close()
    assert box.dropped == 0 and box.qsize() == 2  # the event and one sentinel


# -- loss: a drop is signalled to the consumer like a reconnect ------------------------------------------------------------------------------


class _Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


async def test_a_drop_is_signalled_once_at_the_consumers_next_read():
    clock, losses = _Clock(), []
    box = Mailbox(maxsize=2, on_loss=lambda: losses.append(clock.now), clock=clock)
    for i in range(5):
        box.offer(i)
    assert losses == [], "nothing is signalled until the consumer reads again"
    assert await box.next() == 3  # 0-2 were dropped
    assert losses == [100.0], "signalled before the event is handed over"
    assert await box.next() == 4 and losses == [100.0]


async def test_a_mailbox_that_never_overflowed_never_signals():
    losses = []
    box = Mailbox(maxsize=5, on_loss=lambda: losses.append(1))
    for i in range(5):
        box.offer(i)
    for _ in range(5):
        await box.next()
    assert losses == []


async def test_signals_are_rate_limited_but_a_later_drop_is_never_forgotten():
    clock, losses = _Clock(), []
    box = Mailbox(maxsize=1, on_loss=lambda: losses.append(clock.now), clock=clock, min_signal_interval=5.0)
    box.offer("a"), box.offer("b")
    await box.next()
    assert losses == [100.0]
    clock.now += 1
    box.offer("c"), box.offer("d")  # another drop, inside the interval
    await box.next()
    assert losses == [100.0], "too soon: held back"
    box.offer("e")
    clock.now += 10
    box.offer("f")  # still the same unsignalled loss
    await box.next()
    assert losses == [100.0, 111.0], "the held-back loss is signalled at the first read after the interval"


async def test_a_coalescing_mailbox_does_not_signal_a_loss_it_is_built_to_have():
    losses = []
    box = Mailbox(coalesce=True, on_loss=lambda: losses.append(1))
    for i in range(10):
        box.offer(i)
    assert await box.next() == 9 and losses == []


async def test_a_hook_that_raises_does_not_break_the_consumer():
    def boom():
        raise RuntimeError("flush failed")

    box = Mailbox(maxsize=1, on_loss=boom)
    box.offer(1), box.offer(2)
    assert await box.next() == 2


# -- the invalidation subscriber: a dropped invalidation flushes the caches like a reconnect --------------------------------------------------


class _Stub:
    async def aclose(self) -> None:
        return None


async def test_an_overflowed_invalidation_subscription_calls_its_reconnect_hook():
    bus = InMemoryEventBus(queue_maxsize=3)
    await bus.initialize()
    inv = PostgresInvalidationBus(bus)
    calls: list[int] = []
    handled: list[str] = []

    async def handler(key: str) -> None:
        handled.append(key)

    sub = await inv.subscribe(InvalidationTopic.LLM_PROVIDER, handler, on_reconnect=lambda: calls.append(1))
    for i in range(50):  # published faster than the consumer runs: the bound drops the oldest
        await inv.publish(InvalidationTopic.LLM_PROVIDER, f"p{i}")
    for _ in range(50):
        if len(handled) >= 3:
            break
        await asyncio.sleep(0.01)
    assert handled == ["p47", "p48", "p49"], "only the newest invalidations survived the bound"
    assert calls == [1], "the drop was signalled exactly once, like a reconnect"
    await sub.aclose()
    await bus.aclose()


async def test_an_overflowed_invalidation_flushes_the_registry_caches():
    from primer.api.registries.provider_registry import ProviderRegistry

    bus = InMemoryEventBus(queue_maxsize=3)
    await bus.initialize()
    registry = ProviderRegistry(storage_provider=object())
    await registry.bind_invalidation_bus(PostgresInvalidationBus(bus))
    registry._llm_cache["llm-keep"] = _Stub()  # type: ignore[assignment]
    registry._toolset_cache["ts-keep"] = _Stub()  # type: ignore[assignment]
    for i in range(50):
        await bus.publish(f"invalidate:llm_provider:other-{i}", {})
    for _ in range(100):
        if not registry._llm_cache and not registry._toolset_cache:
            break
        await asyncio.sleep(0.01)
    assert not registry._llm_cache and not registry._toolset_cache, "a dropped invalidation must flush every cache, as a reconnect does"
    await registry.aclose()
    await bus.aclose()
