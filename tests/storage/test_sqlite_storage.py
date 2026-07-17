"""CRUD tests for SqliteStorage."""

from __future__ import annotations

from typing import Optional

import pytest
from pydantic import SecretStr

from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.storage.sqlite import SqliteStorageProvider


class _Doc(Identifiable):
    title: str
    count: int = 0
    note: str | None = None
    secret: SecretStr | None = None
    tags: list[str] | None = None


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


from primer.model.storage import (
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


async def _walk_cursor(
    s, *, order_by: list[OrderBy] | None, length: int
) -> list[str]:
    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = await s.find(None, CursorPage(cursor=cursor, length=length), order_by=order_by)
        assert isinstance(page, CursorPageResponse)
        seen.extend(d.id for d in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return seen


@pytest.mark.asyncio
async def test_cursor_pagination_nullable_orderby_asc_no_drops_no_dupes(
    sqlite_provider: SqliteStorageProvider,
):
    """Keyset pagination ordered on a NULLABLE field must page across the
    NULL boundary without dropping or duplicating rows (NULLs sort last)."""
    s = sqlite_provider.get_storage(_Doc)
    rows = [
        ("d0", "a"), ("d1", None), ("d2", "b"), ("d3", None),
        ("d4", "c"), ("d5", None), ("d6", "d"),
    ]
    for rid, note in rows:
        await s.create(_Doc(id=rid, title="t", note=note))
    seen = await _walk_cursor(
        s, order_by=[OrderBy(field="note", direction="asc")], length=2
    )
    assert sorted(seen) == [f"d{i}" for i in range(7)]
    assert len(seen) == len(set(seen)), f"duplicates: {seen}"
    # NULLs (d1, d3, d5) come last in ASC order.
    assert set(seen[-3:]) == {"d1", "d3", "d5"}
    assert seen[:4] == ["d0", "d2", "d4", "d6"]


@pytest.mark.asyncio
async def test_cursor_pagination_nullable_orderby_desc_no_drops_no_dupes(
    sqlite_provider: SqliteStorageProvider,
):
    s = sqlite_provider.get_storage(_Doc)
    rows = [
        ("d0", "a"), ("d1", None), ("d2", "b"), ("d3", None), ("d4", "c"),
    ]
    for rid, note in rows:
        await s.create(_Doc(id=rid, title="t", note=note))
    seen = await _walk_cursor(
        s, order_by=[OrderBy(field="note", direction="desc")], length=2
    )
    assert sorted(seen) == [f"d{i}" for i in range(5)]
    assert len(seen) == len(set(seen)), f"duplicates: {seen}"
    # Non-null descending first, NULLs last.
    assert seen[:3] == ["d4", "d2", "d0"]
    assert set(seen[-2:]) == {"d1", "d3"}


@pytest.mark.asyncio
async def test_contains_matches_array_membership(
    sqlite_provider: SqliteStorageProvider,
):
    """Op.CONTAINS matches a row whose JSON array field holds the scalar,
    and excludes rows missing the element or with no array at all."""
    s = sqlite_provider.get_storage(_Doc)
    await s.create(_Doc(id="m1", title="a", tags=["ev:1", "ev:2"]))
    await s.create(_Doc(id="m2", title="b", tags=["ev:3"]))
    await s.create(_Doc(id="m3", title="c", tags=None))
    hit = await s.find(
        Predicate(
            left=FieldRef(name="tags"), op=Op.CONTAINS, right=Value(value="ev:2")
        ),
        OffsetPage(offset=0, length=10),
    )
    assert [d.id for d in hit.items] == ["m1"]
    miss = await s.find(
        Predicate(
            left=FieldRef(name="tags"), op=Op.CONTAINS, right=Value(value="ev:9")
        ),
        OffsetPage(offset=0, length=10),
    )
    assert [d.id for d in miss.items] == []


@pytest.mark.asyncio
async def test_like_is_case_sensitive(sqlite_provider: SqliteStorageProvider):
    """Op.LIKE is case-SENSITIVE (Protocol contract), matching Postgres.

    SQLite LIKE is case-insensitive for ASCII by default; the provider
    pins ``PRAGMA case_sensitive_like = ON`` so "Hello" does not match
    "hello%"."""
    s = sqlite_provider.get_storage(_Doc)
    await s.create(_Doc(id="h", title="Hello"))
    miss = await s.find(
        Predicate(left=FieldRef(name="title"), op=Op.LIKE, right=Value(value="hello%")),
        OffsetPage(offset=0, length=10),
    )
    assert [d.id for d in miss.items] == []
    hit = await s.find(
        Predicate(left=FieldRef(name="title"), op=Op.LIKE, right=Value(value="Hello%")),
        OffsetPage(offset=0, length=10),
    )
    assert [d.id for d in hit.items] == ["h"]


@pytest.mark.asyncio
async def test_ilike_is_case_insensitive(sqlite_provider: SqliteStorageProvider):
    """Op.ILIKE matches case-INSENSITIVELY where Op.LIKE (case-sensitive)
    does not.

    SQLite has no ILIKE and pins LIKE case-sensitive; the translator emits
    ``LOWER(field) LIKE LOWER(pattern)`` so "Foo"/"FOO"/"fOo" all match a
    "%foo%" query.
    """
    s = sqlite_provider.get_storage(_Doc)
    await s.create(_Doc(id="a", title="Foo bar"))
    await s.create(_Doc(id="b", title="a FOO stop"))
    await s.create(_Doc(id="c", title="plain foo lower"))
    await s.create(_Doc(id="d", title="unrelated"))
    for pattern in ("%foo%", "%FOO%", "%FoO%"):
        hit = await s.find(
            Predicate(
                left=FieldRef(name="title"), op=Op.ILIKE, right=Value(value=pattern)
            ),
            OffsetPage(offset=0, length=10),
        )
        assert sorted(d.id for d in hit.items) == ["a", "b", "c"], pattern
    # LIKE (case-sensitive) with a lowercased pattern matches ONLY the row that
    # literally contains lowercase "foo" -- the capitalised rows are missed.
    like_hit = await s.find(
        Predicate(left=FieldRef(name="title"), op=Op.LIKE, right=Value(value="%foo%")),
        OffsetPage(offset=0, length=10),
    )
    assert sorted(d.id for d in like_hit.items) == ["c"]


@pytest.mark.asyncio
async def test_ilike_escapes_wildcards(sqlite_provider: SqliteStorageProvider):
    """A literal ``%`` in an ILIKE pattern (escaped as ``\\%``) matches only a
    literal ``%`` -- the ESCAPE '\\' clause must be honoured, not swallowed."""
    s = sqlite_provider.get_storage(_Doc)
    await s.create(_Doc(id="pct", title="50% off"))
    await s.create(_Doc(id="plain", title="50 percent"))
    # Escaped '%' -> literal '%': only the row containing a literal '%'.
    hit = await s.find(
        Predicate(
            left=FieldRef(name="title"),
            op=Op.ILIKE,
            right=Value(value="%50\\%%"),
        ),
        OffsetPage(offset=0, length=10),
    )
    assert [d.id for d in hit.items] == ["pct"]
    # Unescaped '%' would over-match: sanity-check the literal path really
    # discriminates by confirming the plain row is only hit when we let '%'
    # act as a wildcard.
    wild = await s.find(
        Predicate(
            left=FieldRef(name="title"), op=Op.ILIKE, right=Value(value="%50%")
        ),
        OffsetPage(offset=0, length=10),
    )
    assert sorted(d.id for d in wild.items) == ["pct", "plain"]
