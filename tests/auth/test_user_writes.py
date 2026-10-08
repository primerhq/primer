"""write_user_fields is a compare-and-set on session_epoch, retried (SEC-05 review).

Against a real backend (SQLite): a concurrent epoch bump that lands BETWEEN
the helper's read and its guarded write must make that write miss, and the
retry must apply on top of it, so two bumps never collapse into one and the
other writer's bump is never undone.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from primer.auth.user_writes import write_user_fields
from primer.model.except_ import ConflictError
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from primer.model.user import User
from primer.storage._patch import raw_generation
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def users(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "u.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    storage = provider.get_storage(User)
    await storage.create(User(
        id="u1", username="alice", password_hash="$argon2id$old", created_at=datetime.now(timezone.utc),
    ))
    yield storage
    await provider.aclose()


def _interleave(monkeypatch, storage, *, times: int):
    """Before each of the first ``times`` guarded writes, another writer bumps the epoch."""
    real = storage.patch_if
    calls = {"guarded": 0, "injected": 0}

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if "session_epoch" in where and calls["injected"] < times:
            current = await storage.get(id)
            await real(
                id, {"session_epoch": current.session_epoch + 1},
                where={"session_epoch": [raw_generation(current, "session_epoch")]},
            )
            calls["injected"] += 1
        calls["guarded"] += 1
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    monkeypatch.setattr(storage, "patch_if", patch_if)
    return calls


async def test_a_bump_between_the_read_and_the_write_is_kept_and_ours_lands_on_top(users, monkeypatch):
    calls = _interleave(monkeypatch, users, times=1)
    saved = await write_user_fields(users, "u1", {"password_hash": "$argon2id$new"}, bump_epoch=True)
    assert calls == {"guarded": 2, "injected": 1}  # the first guarded write missed, the retry applied
    stored = await users.get("u1")
    assert stored.session_epoch == 2 == saved.session_epoch
    assert stored.password_hash == "$argon2id$new"


async def test_a_field_write_without_a_bump_still_keeps_a_concurrent_bump(users, monkeypatch):
    _interleave(monkeypatch, users, times=1)
    await write_user_fields(users, "u1", {"email": "a@example.com"}, bump_epoch=False)
    stored = await users.get("u1")
    assert stored.session_epoch == 1
    assert stored.email == "a@example.com"


async def test_a_writer_that_never_wins_gives_up_with_a_conflict(users, monkeypatch):
    _interleave(monkeypatch, users, times=100)
    with pytest.raises(ConflictError):
        await write_user_fields(users, "u1", {}, bump_epoch=True)
    assert (await users.get("u1")).session_epoch == 8  # the eight injected bumps, none of ours
