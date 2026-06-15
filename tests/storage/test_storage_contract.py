"""Parametrised :class:`Storage` contract — runs against every backend.

Each scenario is asserted on both Postgres (when ``PRIMER_TEST_PG_DSN``
is set) and SQLite. The point is to catch a semantic divergence the
moment it appears, not to re-test the per-backend translator.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.model.storage import (
    CursorPage,
    FieldRef,
    OffsetPage,
    Op,
    OrderBy,
    Predicate,
    Value,
)
from primer.storage.factory import StorageProviderFactory


class _Thing(Identifiable):
    name: str
    count: int = 0
    status: str = "created"
    workspace_id: str = "w"


_BACKENDS: list[str] = ["sqlite"]
if os.environ.get("PRIMER_TEST_PG_DSN"):
    _BACKENDS.append("postgres")


@pytest_asyncio.fixture(params=_BACKENDS)
async def provider(
    request: pytest.FixtureRequest, tmp_path: Path,
) -> AsyncIterator[StorageProvider]:
    backend = request.param
    if backend == "sqlite":
        cfg = StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=tmp_path / "contract.sqlite"),
        )
    else:
        pytest.skip("postgres contract path requires PRIMER_TEST_PG_DSN")
    p = StorageProviderFactory.create(cfg)
    await p.initialize()
    try:
        yield p
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_get_create_update_delete(provider: StorageProvider) -> None:
    s = provider.get_storage(_Thing)
    assert await s.get("x") is None
    await s.create(_Thing(id="x", name="a", count=1))
    fetched = await s.get("x")
    assert fetched is not None and fetched.name == "a"
    with pytest.raises(ConflictError):
        await s.create(_Thing(id="x", name="dup"))
    updated = await s.update(_Thing(id="x", name="b", count=2))
    assert updated.name == "b"
    await s.delete("x")
    with pytest.raises(NotFoundError):
        await s.delete("x")


@pytest.mark.asyncio
async def test_find_predicate_eq_and_in(provider: StorageProvider) -> None:
    s = provider.get_storage(_Thing)
    for i, name in enumerate(["a", "b", "c"]):
        await s.create(_Thing(id=f"t{i}", name=name, count=i))
    hits = await s.find(
        Predicate(left=FieldRef(name="name"), op=Op.EQ, right=Value(value="b")),
        OffsetPage(offset=0, length=10),
    )
    assert {t.name for t in hits.items} == {"b"}
    hits = await s.find(
        Predicate(
            left=FieldRef(name="count"),
            op=Op.IN,
            right=Value(value=[0, 2]),
        ),
        OffsetPage(offset=0, length=10),
    )
    assert sorted(t.count for t in hits.items) == [0, 2]


@pytest.mark.asyncio
async def test_orderby_and_pagination(provider: StorageProvider) -> None:
    s = provider.get_storage(_Thing)
    for i in range(5):
        await s.create(_Thing(id=f"r{i:02d}", name="x", count=i))
    page1 = await s.list(
        OffsetPage(offset=0, length=2),
        order_by=[OrderBy(field="count", direction="desc")],
    )
    page2 = await s.list(
        OffsetPage(offset=2, length=2),
        order_by=[OrderBy(field="count", direction="desc")],
    )
    counts = [t.count for t in page1.items] + [t.count for t in page2.items]
    assert counts == [4, 3, 2, 1]


@pytest.mark.asyncio
async def test_cursor_walk_terminates_and_visits_each_once(
    provider: StorageProvider,
) -> None:
    """Regression for e2e T0730: a no-predicate cursor walk must
    TERMINATE (final page returns ``next_cursor=None``) and visit
    every row exactly once -- no infinite loop, no repeats, no gaps.

    Pins the keyset-seek invariant: ``next_cursor`` is only emitted
    when a look-ahead row beyond the requested page exists, so the
    final page closes the walk.
    """
    s = provider.get_storage(_Thing)
    seeded = {f"r{i:02d}" for i in range(5)}
    for i in range(5):
        await s.create(_Thing(id=f"r{i:02d}", name="x", count=i))

    seen: list[str] = []
    cursor: str | None = None
    for _page in range(40):  # bounded safety net, mirrors the e2e test
        resp = await s.find(None, CursorPage(cursor=cursor, length=2))
        seen.extend(t.id for t in resp.items)
        cursor = resp.next_cursor
        if cursor is None:
            break
    else:  # pragma: no cover - only hit on the regression we're guarding
        pytest.fail("cursor walk did not terminate within 40 pages")

    # Each seeded id appears exactly once -- no repeats (cursor advanced
    # past the last item) and none missed (the walk covered everything).
    assert sorted(seen) == sorted(seeded)
    assert len(seen) == len(set(seen)), f"cursor walk repeated ids: {seen!r}"


@pytest.mark.asyncio
async def test_find_multi_clause_and_predicate_matches(
    provider: StorageProvider,
) -> None:
    """Regression for e2e T0802: a binary AND predicate
    (``workspace_id == X AND status == S``) must return rows
    matching BOTH clauses and exclude rows matching only one.

    Pins the predicate-composition path / query builder: the AND is
    translated to ``(left) AND (right)`` and matching rows are
    returned (the e2e failure was a stale test premise -- sessions
    never reached the asserted status -- not a query-builder bug;
    this locks the builder's correctness for matching rows).
    """
    s = provider.get_storage(_Thing)
    # Three rows on workspace "wa" with the target status; one on "wa"
    # with a different status; one on "wb" with the target status.
    await s.create(_Thing(id="m0", name="x", status="ended", workspace_id="wa"))
    await s.create(_Thing(id="m1", name="x", status="ended", workspace_id="wa"))
    await s.create(_Thing(id="m2", name="x", status="ended", workspace_id="wa"))
    await s.create(_Thing(id="other-status", name="x", status="created", workspace_id="wa"))
    await s.create(_Thing(id="other-ws", name="x", status="ended", workspace_id="wb"))

    pred = Predicate(
        left=Predicate(
            left=FieldRef(name="workspace_id"),
            op=Op.EQ,
            right=Value(value="wa"),
        ),
        op=Op.AND,
        right=Predicate(
            left=FieldRef(name="status"),
            op=Op.EQ,
            right=Value(value="ended"),
        ),
    )
    hits = await s.find(pred, OffsetPage(offset=0, length=100))
    got = {t.id for t in hits.items}
    assert got == {"m0", "m1", "m2"}, got
    assert "other-status" not in got  # matched only the workspace clause
    assert "other-ws" not in got      # matched only the status clause
