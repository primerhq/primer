"""S6 P3: the correlation store maps threads to sessions.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 5. A record with
kind="session" and tool_call_id=None is a thread mapping; the same record
with tool_call_id set additionally names an OPEN gate.
"""

from __future__ import annotations

from primer.channel.correlation import CorrelationStore
from primer.model.channel_correlation import ChannelCorrelation
from tests.conftest import _FakeStorageProvider


async def test_thread_session_record_has_no_gate():
    store = CorrelationStore(_FakeStorageProvider())
    rec = await store.upsert_thread_session(
        channel_id="ch-1", anchor="thr-1",
        workspace_id="w1", session_id="s1",
    )
    assert rec.kind == "session"
    assert rec.session_id == "s1"
    assert rec.workspace_id == "w1"
    assert rec.tool_call_id is None


async def test_gate_upsert_keeps_the_mapping_and_adds_the_gate():
    store = CorrelationStore(_FakeStorageProvider())
    await store.upsert_thread_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1", session_id="s1",
    )
    await store.upsert_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1",
        session_id="s1", tool_call_id="tc-9",
    )
    rec = await store.lookup("ch-1", "thr-1")
    assert rec.session_id == "s1"
    assert rec.tool_call_id == "tc-9"


async def test_clear_gate_leaves_the_thread_mapped():
    store = CorrelationStore(_FakeStorageProvider())
    await store.upsert_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1",
        session_id="s1", tool_call_id="tc-9",
    )
    await store.clear_gate("ch-1", "thr-1")
    rec = await store.lookup("ch-1", "thr-1")
    assert rec is not None
    assert rec.session_id == "s1"
    assert rec.tool_call_id is None


async def test_clear_for_session_removes_every_mapping():
    sp = _FakeStorageProvider()
    store = CorrelationStore(sp)
    await store.upsert_thread_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1", session_id="s1",
    )
    await store.upsert_thread_session(
        channel_id="ch-2", anchor="thr-2", workspace_id="w1", session_id="s1",
    )
    await store.upsert_thread_session(
        channel_id="ch-3", anchor="thr-3", workspace_id="w1", session_id="s2",
    )
    removed = await store.clear_for_session("s1")
    assert removed == 2
    assert await store.lookup("ch-1", "thr-1") is None
    assert await store.lookup("ch-3", "thr-3") is not None
