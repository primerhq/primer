"""hotpaths #1: _find_thread_chat resolves via the CorrelationStore fast path.

Proves the keyed lookup returns the same chat the historical full scan would,
and that the slow-path scan fallback still fires (and refreshes the
correlation) for legacy chats that have no correlation record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.chat_router import ChatChannelRouter
from primer.channel.correlation import CorrelationStore
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def _provider(tmp_path: Path) -> SqliteStorageProvider:
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    return p


@pytest.mark.asyncio
async def test_fast_path_uses_correlation_record(tmp_path: Path):
    """A correlation record maps the thread anchor straight to its chat."""
    p = await _provider(tmp_path)
    now = datetime.now(timezone.utc)
    binding = ChatChannelBinding(channel_id="ch-1", thread_external_id="th-1")
    chat = Chat(id="chat-live", agent_id="a", created_at=now,
                channel_binding=binding)
    await p.get_storage(Chat).create(chat)
    store = CorrelationStore(p)
    await store.upsert_chat(channel_id="ch-1", anchor="th-1", chat_id="chat-live")

    r = ChatChannelRouter(storage_provider=p, correlation_store=store)
    found = await r._find_thread_chat(channel_id="ch-1", thread_external_id="th-1")
    assert found is not None and found.id == "chat-live"


@pytest.mark.asyncio
async def test_fast_path_ignores_ended_correlated_chat_then_scans(tmp_path: Path):
    """A correlation pointing at an ended chat must not be returned; the scan
    finds the live chat sharing the binding (equivalence with old behaviour)."""
    p = await _provider(tmp_path)
    now = datetime.now(timezone.utc)
    binding = ChatChannelBinding(channel_id="ch-1", thread_external_id="th-1")
    ended = Chat(id="chat-ended", agent_id="a", created_at=now,
                 status="ended", channel_binding=binding)
    live = Chat(id="chat-live", agent_id="a", created_at=now,
                channel_binding=binding)
    await p.get_storage(Chat).create(ended)
    await p.get_storage(Chat).create(live)
    store = CorrelationStore(p)
    # Stale correlation points at the ENDED chat.
    await store.upsert_chat(channel_id="ch-1", anchor="th-1", chat_id="chat-ended")

    r = ChatChannelRouter(storage_provider=p, correlation_store=store)
    found = await r._find_thread_chat(channel_id="ch-1", thread_external_id="th-1")
    assert found is not None and found.id == "chat-live"
    # The slow path refreshed the correlation to the live chat.
    refreshed = await store.lookup("ch-1", "th-1")
    assert refreshed is not None and refreshed.chat_id == "chat-live"


@pytest.mark.asyncio
async def test_no_correlation_falls_back_to_scan(tmp_path: Path):
    """Legacy chats with no correlation still resolve via the scan and the
    scan's hit seeds a correlation for the next lookup."""
    p = await _provider(tmp_path)
    now = datetime.now(timezone.utc)
    binding = ChatChannelBinding(channel_id="ch-1", thread_external_id="th-1")
    live = Chat(id="chat-live", agent_id="a", created_at=now,
                channel_binding=binding)
    await p.get_storage(Chat).create(live)

    store = CorrelationStore(p)
    r = ChatChannelRouter(storage_provider=p, correlation_store=store)
    found = await r._find_thread_chat(channel_id="ch-1", thread_external_id="th-1")
    assert found is not None and found.id == "chat-live"
    seeded = await store.lookup("ch-1", "th-1")
    assert seeded is not None and seeded.chat_id == "chat-live"


@pytest.mark.asyncio
async def test_no_match_returns_none(tmp_path: Path):
    """No chat and no correlation -> None (unchanged)."""
    p = await _provider(tmp_path)
    store = CorrelationStore(p)
    r = ChatChannelRouter(storage_provider=p, correlation_store=store)
    found = await r._find_thread_chat(channel_id="ch-1", thread_external_id="nope")
    assert found is None
