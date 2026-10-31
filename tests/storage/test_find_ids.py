"""``Storage.find_ids``: ``(id, access)`` of every match, ordered by id, keyset-paged (perf plan Task 4, stage A)."""
from __future__ import annotations

import pytest

from primer.access.policy import visible_predicate
from primer.access.person import Person
from primer.model.access import Access
from primer.model.collection import Collection
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import needs_postgres

pg = needs_postgres("find_ids on Postgres")


async def _seed(store, prefix: str) -> None:
    for i in range(7):
        await store.create(Collection(id=f"{prefix}-{i}", description="d", access=Access(owners=["u-a"], grants={"u-b": "read"} if i % 2 else {}, public=i == 6)))


async def _check(store, prefix: str) -> None:
    allrows = await store.find_ids(None)
    mine = [(i, a) for i, a in allrows if i.startswith(prefix)]
    assert [i for i, _ in mine] == [f"{prefix}-{n}" for n in range(7)], "ordered by id"
    assert mine[1][1]["grants"] == {"u-b": "read"} and mine[0][1]["owners"] == ["u-a"] and mine[6][1]["public"] is True
    scoped = await store.find_ids(visible_predicate(Person("u-b", "user")))
    assert sorted(i for i, _ in scoped if i.startswith(prefix)) == [f"{prefix}-1", f"{prefix}-3", f"{prefix}-5", f"{prefix}-6"]


@pytest.mark.asyncio
async def test_sqlite_find_ids_returns_id_and_access_ordered_and_filtered(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await sp.initialize()
    await _seed(sp.get_storage(Collection), "kb")
    await _check(sp.get_storage(Collection), "kb")
    await sp.aclose()


@pytest.mark.asyncio
async def test_sqlite_find_ids_keyset_pages_without_skipping_or_repeating(tmp_path, monkeypatch):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await sp.initialize()
    store = sp.get_storage(Collection)
    monkeypatch.setattr(type(store), "_IDS_PAGE", 3)
    await _seed(store, "kb")
    assert [i for i, _ in await store.find_ids(None)] == [f"kb-{n}" for n in range(7)]
    await sp.aclose()


@pg
@pytest.mark.asyncio
async def test_postgres_find_ids_matches_sqlite_and_keyset_pages(postgres_storage_provider, monkeypatch):
    store = postgres_storage_provider.get_storage(Collection)
    prefix = "perf-t4-kb"
    monkeypatch.setattr(type(store), "_IDS_PAGE", 3)
    await _seed(store, prefix)
    try:
        await _check(store, prefix)
    finally:
        for n in range(7):
            await store.delete(f"{prefix}-{n}")
