"""CRUD tests for SqliteStorage."""

from __future__ import annotations

from typing import Optional

import pytest
from pydantic import SecretStr

from matrix.model.common import Identifiable
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.storage.sqlite import SqliteStorageProvider


class _Doc(Identifiable):
    title: str
    count: int = 0
    note: Optional[str] = None
    secret: SecretStr | None = None


@pytest.mark.asyncio
async def test_get_missing_returns_none(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Doc)
    assert await s.get("nope") is None


@pytest.mark.asyncio
async def test_create_then_get_roundtrips(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Doc)
    saved = await s.create(_Doc(id="a", title="hello", count=3))
    assert saved.id == "a"
    assert saved.title == "hello"
    assert saved.count == 3
    fetched = await s.get("a")
    assert fetched is not None
    assert fetched.title == "hello"


@pytest.mark.asyncio
async def test_create_duplicate_raises_conflict(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Doc)
    await s.create(_Doc(id="dup", title="first"))
    with pytest.raises(ConflictError):
        await s.create(_Doc(id="dup", title="second"))


@pytest.mark.asyncio
async def test_update_replaces_and_returns(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Doc)
    await s.create(_Doc(id="u", title="old", count=1))
    updated = await s.update(_Doc(id="u", title="new", count=2))
    assert updated.title == "new" and updated.count == 2
    fetched = await s.get("u")
    assert fetched is not None and fetched.title == "new"


@pytest.mark.asyncio
async def test_update_missing_raises_notfound(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Doc)
    with pytest.raises(NotFoundError):
        await s.update(_Doc(id="ghost", title="x"))


@pytest.mark.asyncio
async def test_delete_removes_row(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Doc)
    await s.create(_Doc(id="rm", title="bye"))
    await s.delete("rm")
    assert await s.get("rm") is None


@pytest.mark.asyncio
async def test_delete_missing_raises_notfound(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Doc)
    with pytest.raises(NotFoundError):
        await s.delete("ghost")


@pytest.mark.asyncio
async def test_secretstr_roundtrips_via_storage(
    sqlite_provider: SqliteStorageProvider,
):
    s = sqlite_provider.get_storage(_Doc)
    await s.create(_Doc(id="s", title="t", secret=SecretStr("hunter2")))
    fetched = await s.get("s")
    assert fetched is not None
    assert isinstance(fetched.secret, SecretStr)
    assert fetched.secret.get_secret_value() == "hunter2"


from matrix.model.storage import (
    CursorPage,
    CursorPageResponse,
    FieldRef,
    OffsetPage,
    OffsetPageResponse,
    Op,
    OrderBy,
    Predicate,
    Value,
)


@pytest.mark.asyncio
async def test_list_offset_returns_total_and_items(
    sqlite_provider: SqliteStorageProvider,
):
    s = sqlite_provider.get_storage(_Doc)
    for i in range(5):
        await s.create(_Doc(id=f"d{i}", title=f"t{i}", count=i))
    page = await s.list(OffsetPage(offset=0, length=3))
    assert isinstance(page, OffsetPageResponse)
    assert page.total == 5
    assert page.length == 3
    assert [d.id for d in page.items] == ["d0", "d1", "d2"]


@pytest.mark.asyncio
async def test_list_offset_with_orderby_desc(
    sqlite_provider: SqliteStorageProvider,
):
    s = sqlite_provider.get_storage(_Doc)
    for i in range(3):
        await s.create(_Doc(id=f"d{i}", title=f"t{i}", count=i))
    page = await s.list(
        OffsetPage(offset=0, length=10),
        order_by=[OrderBy(field="count", direction="desc")],
    )
    assert [d.count for d in page.items] == [2, 1, 0]


@pytest.mark.asyncio
async def test_find_predicate_eq(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Doc)
    await s.create(_Doc(id="a", title="hit", count=1))
    await s.create(_Doc(id="b", title="miss", count=2))
    page = await s.find(
        Predicate(left=FieldRef(name="title"), op=Op.EQ, right=Value(value="hit")),
        OffsetPage(offset=0, length=10),
    )
    assert [d.id for d in page.items] == ["a"]


@pytest.mark.asyncio
async def test_find_predicate_gt_int_cast(
    sqlite_provider: SqliteStorageProvider,
):
    s = sqlite_provider.get_storage(_Doc)
    for i in range(5):
        await s.create(_Doc(id=f"d{i}", title="t", count=i))
    page = await s.find(
        Predicate(left=FieldRef(name="count"), op=Op.GT, right=Value(value=2)),
        OffsetPage(offset=0, length=10),
    )
    assert sorted(d.count for d in page.items) == [3, 4]


@pytest.mark.asyncio
async def test_find_in_list(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Doc)
    for i in range(4):
        await s.create(_Doc(id=f"d{i}", title="t", count=i))
    page = await s.find(
        Predicate(
            left=FieldRef(name="count"),
            op=Op.IN,
            right=Value(value=[1, 3]),
        ),
        OffsetPage(offset=0, length=10),
    )
    assert sorted(d.count for d in page.items) == [1, 3]


@pytest.mark.asyncio
async def test_cursor_pagination_walks_all_items(
    sqlite_provider: SqliteStorageProvider,
):
    s = sqlite_provider.get_storage(_Doc)
    for i in range(7):
        await s.create(_Doc(id=f"d{i:02d}", title="t", count=i))
    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = await s.list(CursorPage(cursor=cursor, length=3))
        assert isinstance(page, CursorPageResponse)
        seen.extend(d.id for d in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert seen == [f"d{i:02d}" for i in range(7)]
