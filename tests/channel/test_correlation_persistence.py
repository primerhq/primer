import pytest
from pathlib import Path
from primer.model.channel_correlation import ChannelCorrelation
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

@pytest.mark.asyncio
async def test_channel_correlation_round_trip(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "x.sqlite"))
    await sp.initialize()
    st = sp.get_storage(ChannelCorrelation)
    row = ChannelCorrelation(id="channel-correlation-1", channel_id="ch-1",
                             anchor="th-1", kind="session", workspace_id="ws-1",
                             session_id="s-1", tool_call_id="tc-1")
    await st.create(row)
    got = await st.get("channel-correlation-1")
    assert got is not None
    assert got.kind == "session"
    assert got.session_id == "s-1"
    assert got.tool_call_id == "tc-1"
    assert got.channel_id == "ch-1" and got.anchor == "th-1"
