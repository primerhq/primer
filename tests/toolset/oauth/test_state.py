"""Tests for primer.toolset.oauth.state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.model.except_ import BadRequestError
from primer.toolset.oauth.state import (
    InMemoryStateStore,
    OAuthState,
)


def _state(principal: str | None = "user-1") -> OAuthState:
    return OAuthState(
        principal=principal,
        toolset_id="ts1",
        code_verifier="verifier-abc",
        spec_version="2025-06-18",
        auth_server_metadata_url="https://idp.example/.well-known/oauth-authorization-server",
        issued_at=datetime.now(timezone.utc),
    )


class TestOAuthState:
    def test_construction(self) -> None:
        s = _state()
        assert s.principal == "user-1"
        assert s.toolset_id == "ts1"
        assert s.code_verifier.get_secret_value() == "verifier-abc"
        assert s.spec_version == "2025-06-18"

    def test_principal_can_be_none(self) -> None:
        s = _state(principal=None)
        assert s.principal is None


class TestInMemoryStateStore:
    async def test_put_returns_uuid_string(self) -> None:
        store = InMemoryStateStore()
        sid = await store.put(_state(), ttl=timedelta(seconds=60))
        assert isinstance(sid, str)
        assert len(sid) >= 32

    async def test_take_returns_payload_then_consumes(self) -> None:
        store = InMemoryStateStore()
        sid = await store.put(_state(), ttl=timedelta(seconds=60))
        got = await store.take(sid)
        assert got.toolset_id == "ts1"
        with pytest.raises(BadRequestError):
            await store.take(sid)

    async def test_take_unknown_state_raises(self) -> None:
        store = InMemoryStateStore()
        with pytest.raises(BadRequestError):
            await store.take("does-not-exist")

    async def test_take_expired_raises_and_evicts(self) -> None:
        store = InMemoryStateStore()
        sid = await store.put(_state(), ttl=timedelta(seconds=-1))
        with pytest.raises(BadRequestError):
            await store.take(sid)

    async def test_distinct_states_have_distinct_ids(self) -> None:
        store = InMemoryStateStore()
        sid1 = await store.put(_state(), ttl=timedelta(seconds=60))
        sid2 = await store.put(_state(), ttl=timedelta(seconds=60))
        assert sid1 != sid2

    async def test_concurrent_put_safe(self) -> None:
        import asyncio

        store = InMemoryStateStore()
        ids = await asyncio.gather(
            *(store.put(_state(), ttl=timedelta(seconds=60)) for _ in range(50))
        )
        assert len(set(ids)) == 50
