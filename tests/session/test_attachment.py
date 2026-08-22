"""Client attachment lifecycle: TTL, heartbeat, high-water mark (S3 s4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from primer.model.client_attachment import ClientAttachment
from primer.session.attachment import (
    ATTACH_TTL_SECONDS,
    attach_or_refresh,
    detach,
    live_attachments,
)
from tests.conftest import _InMemoryStorage


def _storage() -> _InMemoryStorage:
    return _InMemoryStorage(ClientAttachment)


def _now() -> datetime:
    return datetime(2026, 8, 16, 10, 0, 0, tzinfo=UTC)


async def test_attach_captures_the_high_water_mark_and_ttl() -> None:
    st = _storage()
    row = await attach_or_refresh(
        st,
        workspace_id="ws-1",
        session_id="s-1",
        client_id="tab-a",
        last_seq=7,
        now=_now(),
    )
    assert row.attached_seq == 7
    assert row.workspace_id == "ws-1"
    assert row.expires_at == _now() + timedelta(seconds=ATTACH_TTL_SECONDS)
    assert row.id.startswith("att-")


async def test_heartbeat_extends_ttl_without_moving_the_mark() -> None:
    st = _storage()
    first = await attach_or_refresh(
        st,
        workspace_id="ws-1",
        session_id="s-1",
        client_id="tab-a",
        last_seq=7,
        now=_now(),
    )
    later = _now() + timedelta(seconds=10)
    second = await attach_or_refresh(
        st,
        workspace_id="ws-1",
        session_id="s-1",
        client_id="tab-a",
        last_seq=99,
        now=later,
    )
    assert second.id == first.id
    assert second.attached_seq == 7, "a heartbeat must not move the fence"
    assert second.expires_at == later + timedelta(seconds=ATTACH_TTL_SECONDS)


async def test_expired_rows_are_not_live_and_are_swept() -> None:
    st = _storage()
    await attach_or_refresh(
        st,
        workspace_id="ws-1",
        session_id="s-1",
        client_id="tab-a",
        last_seq=1,
        now=_now(),
    )
    after_ttl = _now() + timedelta(seconds=ATTACH_TTL_SECONDS + 1)
    assert await live_attachments(st, "s-1", now=after_ttl) == []
    assert await live_attachments(st, "s-1", now=after_ttl) == []
    # A fresh attach after expiry re-fences at the CURRENT high-water mark.
    fresh = await attach_or_refresh(
        st,
        workspace_id="ws-1",
        session_id="s-1",
        client_id="tab-a",
        last_seq=42,
        now=after_ttl,
    )
    assert fresh.attached_seq == 42


async def test_live_attachments_scope_by_session() -> None:
    st = _storage()
    await attach_or_refresh(
        st,
        workspace_id="ws-1",
        session_id="s-1",
        client_id="tab-a",
        last_seq=1,
        now=_now(),
    )
    await attach_or_refresh(
        st,
        workspace_id="ws-1",
        session_id="s-2",
        client_id="tab-b",
        last_seq=1,
        now=_now(),
    )
    live = await live_attachments(st, "s-1", now=_now())
    assert [r.client_id for r in live] == ["tab-a"]


async def test_detach_removes_only_that_client() -> None:
    st = _storage()
    for client in ("tab-a", "tab-b"):
        await attach_or_refresh(
            st,
            workspace_id="ws-1",
            session_id="s-1",
            client_id=client,
            last_seq=1,
            now=_now(),
        )
    assert await detach(st, session_id="s-1", client_id="tab-a") is True
    assert await detach(st, session_id="s-1", client_id="tab-a") is False
    live = await live_attachments(st, "s-1", now=_now())
    assert [r.client_id for r in live] == ["tab-b"]
