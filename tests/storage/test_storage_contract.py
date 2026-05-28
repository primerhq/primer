"""Parametrised :class:`Storage` contract — runs against every backend.

Each scenario is asserted on both Postgres (when ``MATRIX_TEST_PG_DSN``
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


_BACKENDS: list[str] = ["sqlite"]
if os.environ.get("MATRIX_TEST_PG_DSN"):
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
        pytest.skip("postgres contract path requires MATRIX_TEST_PG_DSN")
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
