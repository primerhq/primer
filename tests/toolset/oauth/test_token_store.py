"""Tests for matrix.toolset.oauth.token_store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from matrix.toolset.oauth.token_store import (
    InMemoryTokenStore,
    TokenRecord,
)


def _record(seconds_to_live: int = 3600, refresh: str | None = "rt") -> TokenRecord:
    return TokenRecord(
        access_token="at",
        refresh_token=refresh,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds_to_live),
        token_type="Bearer",
    )


class TestTokenRecord:
    def test_default_token_type(self) -> None:
        r = TokenRecord(
            access_token="at",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        assert r.token_type == "Bearer"
        assert r.refresh_token is None

    def test_access_token_is_secret(self) -> None:
        r = TokenRecord(
            access_token="hush",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        assert r.access_token.get_secret_value() == "hush"
        assert "hush" not in repr(r)


class TestInMemoryTokenStore:
    async def test_set_then_get_returns_record(self) -> None:
        store = InMemoryTokenStore()
        rec = _record()
        await store.set("k", rec)
        got = await store.get("k")
        assert got is not None
        assert got.access_token.get_secret_value() == "at"

    async def test_get_missing_returns_none(self) -> None:
        store = InMemoryTokenStore()
        assert await store.get("nope") is None

    async def test_delete_removes(self) -> None:
        store = InMemoryTokenStore()
        await store.set("k", _record())
        await store.delete("k")
        assert await store.get("k") is None

    async def test_delete_missing_is_silent(self) -> None:
        store = InMemoryTokenStore()
        await store.delete("never-existed")  # no exception

    async def test_expired_record_evicted_on_get(self) -> None:
        store = InMemoryTokenStore()
        rec = _record(seconds_to_live=-10)
        await store.set("k", rec)
        assert await store.get("k") is None

    async def test_distinct_keys_isolated(self) -> None:
        store = InMemoryTokenStore()
        rec_a = TokenRecord(
            access_token="A",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        rec_b = TokenRecord(
            access_token="B",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        await store.set("a", rec_a)
        await store.set("b", rec_b)
        assert (await store.get("a")).access_token.get_secret_value() == "A"
        assert (await store.get("b")).access_token.get_secret_value() == "B"

    async def test_set_overwrites(self) -> None:
        store = InMemoryTokenStore()
        await store.set("k", _record())
        new_rec = TokenRecord(
            access_token="new",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        await store.set("k", new_rec)
        got = await store.get("k")
        assert got.access_token.get_secret_value() == "new"

    async def test_concurrent_set_safe(self) -> None:
        import asyncio

        store = InMemoryTokenStore()

        async def writer(i: int) -> None:
            await store.set(f"k{i}", _record())

        await asyncio.gather(*(writer(i) for i in range(50)))
        for i in range(50):
            assert await store.get(f"k{i}") is not None
