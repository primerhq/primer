import asyncio
import pytest
from pathlib import Path
from primer.channel.correlation import (
    CorrelationStore,
    ACTIVE_CHAT_ANCHOR,
    _TABLE,
    _UNIQUE_INDEX,
)
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

async def _store(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "c.sqlite"))
    await sp.initialize()
    return CorrelationStore(sp)

@pytest.mark.asyncio
async def test_upsert_and_lookup_session(tmp_path: Path):
    s = await _store(tmp_path)
    await s.upsert_session(channel_id="ch-1", anchor="th-1", workspace_id="ws-1",
                           session_id="s-1", tool_call_id="tc-1")
    rec = await s.lookup("ch-1", "th-1")
    assert rec.kind == "session" and rec.session_id == "s-1" and rec.tool_call_id == "tc-1"

@pytest.mark.asyncio
async def test_upsert_updates_tool_call_same_anchor(tmp_path: Path):
    s = await _store(tmp_path)
    await s.upsert_session(channel_id="ch-1", anchor="th-1", workspace_id="ws-1",
                           session_id="s-1", tool_call_id="tc-1")
    await s.upsert_session(channel_id="ch-1", anchor="th-1", workspace_id="ws-1",
                           session_id="s-1", tool_call_id="tc-2")
    rec = await s.lookup("ch-1", "th-1")
    assert rec.tool_call_id == "tc-2"
    page = await s.list_for_channel("ch-1")
    assert len(page) == 1  # upsert, not insert

@pytest.mark.asyncio
async def test_active_chat_helpers(tmp_path: Path):
    s = await _store(tmp_path)
    await s.set_active_chat("ch-1", "chat-1")
    rec = await s.lookup("ch-1", ACTIVE_CHAT_ANCHOR)
    assert rec.chat_id == "chat-1"

@pytest.mark.asyncio
async def test_upsert_chat_and_clear(tmp_path: Path):
    s = await _store(tmp_path)
    await s.upsert_chat(channel_id="ch-1", anchor="th-9", chat_id="chat-9")
    assert (await s.lookup("ch-1", "th-9")).chat_id == "chat-9"
    await s.clear("ch-1", "th-9")
    assert await s.lookup("ch-1", "th-9") is None

@pytest.mark.asyncio
async def test_lookup_missing_returns_none(tmp_path: Path):
    s = await _store(tmp_path)
    assert await s.lookup("ch-x", "nope") is None


@pytest.mark.asyncio
async def test_unique_index_created_on_channel_anchor(tmp_path: Path):
    """The (channel_id, anchor) unique index is created on first upsert."""
    s = await _store(tmp_path)
    await s.upsert_session(channel_id="ch-1", anchor="th-1", workspace_id="ws-1",
                           session_id="s-1", tool_call_id="tc-1")
    cur = await s._sp.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (_UNIQUE_INDEX,),
    )
    row = await cur.fetchone()
    assert row is not None, "unique index must exist after upsert"
    # And it is genuinely unique on the JSONB-extracted columns.
    cur = await s._sp.connection.execute(
        f"PRAGMA index_info('{_UNIQUE_INDEX}')"
    )
    assert len(await cur.fetchall()) == 2


@pytest.mark.asyncio
async def test_concurrent_upsert_session_resolves_to_single_row(tmp_path: Path):
    """Two concurrent upserts on the same (channel_id, anchor) converge to
    one row -- the double-resume race is closed (the ON CONFLICT path)."""
    s = await _store(tmp_path)
    # Pre-create the index so neither coroutine loses the DDL race; the
    # atomic INSERT ... ON CONFLICT is what we are exercising here.
    await s._ensure_unique_index()

    async def _w(tc: str):
        return await s.upsert_session(
            channel_id="ch-1", anchor="th-1", workspace_id="ws-1",
            session_id="s-1", tool_call_id=tc,
        )

    results = await asyncio.gather(_w("tc-a"), _w("tc-b"))
    # Both writers persisted; the store collapsed onto one row.
    rows = await s.list_for_channel("ch-1")
    assert len(rows) == 1
    # The surviving row carries one of the two tool_call_ids (last writer
    # wins) and a single, stable id shared by both returned records.
    assert {r.id for r in results} == {rows[0].id}
    assert rows[0].tool_call_id in {"tc-a", "tc-b"}


@pytest.mark.asyncio
async def test_concurrent_upsert_chat_resolves_to_single_row(tmp_path: Path):
    s = await _store(tmp_path)
    await s._ensure_unique_index()

    async def _w(cid: str):
        return await s.upsert_chat(channel_id="ch-1", anchor="th-1", chat_id=cid)

    await asyncio.gather(_w("chat-a"), _w("chat-b"))
    rows = await s.list_for_channel("ch-1")
    assert len(rows) == 1
    assert rows[0].chat_id in {"chat-a", "chat-b"}


@pytest.mark.asyncio
async def test_upsert_preserves_id_on_conflict(tmp_path: Path):
    """ON CONFLICT keeps the original row id (not the second writer's)."""
    s = await _store(tmp_path)
    first = await s.upsert_session(channel_id="ch-1", anchor="th-1",
                                   workspace_id="ws-1", session_id="s-1",
                                   tool_call_id="tc-1")
    second = await s.upsert_session(channel_id="ch-1", anchor="th-1",
                                    workspace_id="ws-1", session_id="s-1",
                                    tool_call_id="tc-2")
    assert second.id == first.id
    assert second.tool_call_id == "tc-2"
    assert len(await s.list_for_channel("ch-1")) == 1
