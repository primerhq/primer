"""YieldEventListener: multi-event parks wake on any member key."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.bus.listener import YieldEventListener
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding, SessionStatus, WorkspaceSession,
)
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio


class _Event:
    def __init__(self, event_key, payload):
        self.event_key = event_key
        self.payload = payload


async def _build(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    storage = provider.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=storage)})
    listener = YieldEventListener(bus=None, session_storage=storage, engine=engine)
    return storage, engine, listener


def _multi(keys, primary):
    s = WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.WAITING, created_at=datetime.now(timezone.utc))
    s.parked_status = "parked"
    s.parked_event_key = primary
    s.parked_event_keys = keys
    s.parked_at = datetime.now(timezone.utc)
    s.parked_state = {"yielded": {"tool_name": "_approval", "event_keys": keys}}
    return s


async def test_wakes_on_non_primary_member_key(tmp_path):
    storage, engine, listener = await _build(tmp_path)
    await storage.create(_multi(["ask_user:s1:tc1", "ask_user:s1:tc2"], "ask_user:s1:tc1"))
    # Fire the SECOND key (not the primary parked_event_key).
    await listener._handle_event(_Event("ask_user:s1:tc2", {"response": "blue"}))
    got = await storage.get("s1")
    assert got.parked_status == "resumable"
    assert got.parked_state["resume_event_key"] == "ask_user:s1:tc2"
    assert got.parked_state["resume_event_payload"] == {"response": "blue"}


async def test_single_event_path_unchanged_records_fired_key(tmp_path):
    storage, engine, listener = await _build(tmp_path)
    s = WorkspaceSession(
        id="s2", workspace_id="w1", binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.WAITING, created_at=datetime.now(timezone.utc))
    s.parked_status = "parked"
    s.parked_event_key = "ask_user:s2:tc9"
    s.parked_at = datetime.now(timezone.utc)
    s.parked_state = {"yielded": {"tool_name": "ask_user"}}
    await storage.create(s)
    await listener._handle_event(_Event("ask_user:s2:tc9", {"response": "x"}))
    got = await storage.get("s2")
    assert got.parked_status == "resumable"
    assert got.parked_state["resume_event_key"] == "ask_user:s2:tc9"


async def test_multi_event_accumulates_concurrent_replies(tmp_path):
    """Two replies to a multi-event park (the 2nd while already 'resumable')
    are both recorded in resume_event_payloads, not dropped/overwritten."""
    storage, engine, listener = await _build(tmp_path)
    await storage.create(_multi(["ask_user:s1:tc1", "ask_user:s1:tc2"], "ask_user:s1:tc1"))
    # Reply 1 (primary, while parked) -> flips to resumable.
    await listener._handle_event(_Event("ask_user:s1:tc1", {"response": "fruit"}))
    # Reply 2 (member, while ALREADY resumable) -> must accumulate, not drop.
    await listener._handle_event(_Event("ask_user:s1:tc2", {"response": "color"}))
    got = await storage.get("s1")
    payloads = got.parked_state.get("resume_event_payloads")
    assert set(payloads.keys()) == {"tc1", "tc2"}
    assert payloads["tc1"]["payload"] == {"response": "fruit"}
    assert payloads["tc2"]["payload"] == {"response": "color"}


async def test_single_event_does_not_accumulate(tmp_path):
    """A single-event park keeps the singular field, no map, only advances
    from 'parked' (unchanged behavior)."""
    storage, engine, listener = await _build(tmp_path)
    s = WorkspaceSession(
        id="s9", workspace_id="w1", binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.WAITING, created_at=datetime.now(timezone.utc))
    s.parked_status = "parked"; s.parked_event_key = "ask_user:s9:tcX"
    s.parked_at = datetime.now(timezone.utc)
    s.parked_state = {"yielded": {"tool_name": "ask_user"}}
    await storage.create(s)
    await listener._handle_event(_Event("ask_user:s9:tcX", {"response": "x"}))
    got = await storage.get("s9")
    assert got.parked_status == "resumable"
    assert "resume_event_payloads" not in got.parked_state
    assert got.parked_state["resume_event_payload"] == {"response": "x"}
