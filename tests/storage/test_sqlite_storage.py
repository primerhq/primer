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
