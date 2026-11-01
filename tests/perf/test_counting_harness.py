"""The counting wrappers are transparent and count what they should (Task 0 of the perf plan)."""
from __future__ import annotations

from datetime import UTC, datetime

from primer.model.access import Access
from primer.model.storage import OffsetPage
from primer.model.user import User
from tests.conftest import _FakeStorageProvider
from tests.perf.counting import CountingEmbedder, CountingStorageProvider, CountingVectorStore, loop_ticks

NOW = datetime(2026, 10, 31, tzinfo=UTC)


async def _users(sp, n: int) -> None:
    for i in range(n):
        await sp.get_storage(User).create(User(id=f"u{i}", username=f"user{i}", created_at=NOW, role="user"))


async def test_the_wrapper_forwards_and_returns_the_same_results():
    plain = _FakeStorageProvider()
    wrapped = CountingStorageProvider(_FakeStorageProvider())
    for sp in (plain, wrapped):
        await _users(sp, 5)
    a = await plain.get_storage(User).list(OffsetPage(offset=0, length=3))
    b = await wrapped.get_storage(User).list(OffsetPage(offset=0, length=3))
    assert [u.id for u in a.items] == [u.id for u in b.items] and a.total == b.total
    assert (await wrapped.get_storage(User).get("u1")).username == "user1"
    assert await wrapped.get_storage(User).get("nope") is None


async def test_calls_and_rows_are_counted_only_inside_a_counted_block():
    sp = CountingStorageProvider(_FakeStorageProvider())
    await _users(sp, 5)  # outside the block: not counted
    with sp.counted() as c:
        await sp.get_storage(User).get("u1")
        await sp.get_storage(User).get("missing")
        await sp.get_storage(User).list(OffsetPage(offset=0, length=3))
        await sp.get_event_store().max_id()
    assert c.calls("get", model="User") == 2
    assert c.calls("list", model="User") == 1
    assert c.calls(label="events") == 1
    assert c.rows == 1 + 3  # one hit, one miss, a page of three
    assert c.total == 4
    await sp.get_storage(User).get("u2")  # after the block: not counted
    assert c.total == 4


async def test_blocks_nest_and_each_keeps_its_own_tally():
    sp = CountingStorageProvider(_FakeStorageProvider())
    await _users(sp, 2)
    with sp.counted() as outer:
        await sp.get_storage(User).get("u0")
        with sp.counted() as inner:
            await sp.get_storage(User).get("u1")
        await sp.get_storage(User).get("u0")
    assert inner.total == 1 and outer.total == 2


async def test_embedder_and_vector_store_doubles_count_their_calls():
    class _Embedder:
        async def embed(self, inputs):
            return [[0.0] for _ in inputs]

    class _Store:
        async def put(self, record):
            return None

    emb, store = CountingEmbedder(_Embedder()), CountingVectorStore(_Store())
    with emb.counted() as ce, store.counted() as cs:
        assert await emb.embed(["a", "b"]) == [[0.0], [0.0]]
        await store.put(object())
        await store.put(object())
    assert ce.calls("embed") == 1 and cs.calls("put") == 2


async def test_loop_ticks_complete_alongside_other_work():
    import asyncio

    order: list[str] = []

    async def work():
        await asyncio.sleep(0.015)
        order.append("work")

    async def ticker():
        n = await loop_ticks(3, 0.01)
        order.append("ticks")
        return n

    _, n = await asyncio.gather(work(), ticker())
    assert n == 3 and order[0] == "work"
