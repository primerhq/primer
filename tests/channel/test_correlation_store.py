import pytest
from pathlib import Path
from primer.channel.correlation import CorrelationStore, ACTIVE_CHAT_ANCHOR
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
