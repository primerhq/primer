"""Unit tests for the new YieldEventListener (storage + engine path).

Spec: ``docs/superpowers/specs/2026-06-07-f10c-session-yield-park-resume-design.md``.

The listener now re-points at (bus, session_storage, engine) instead of
(bus, scheduler).  For each event it finds sessions parked on the
event_key, flips them parked -> resumable (stamping resume_event_payload),
then calls engine.mark_resumable so the claim loop picks them up.
"""
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
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Minimal stand-in for a bus Event (matches primer.int.event_bus.Event shape)
# ---------------------------------------------------------------------------

class _Event:
    def __init__(self, event_key: str, payload: dict) -> None:
        self.event_key = event_key
        self.payload = payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_parked_session(session_id: str, event_key: str) -> WorkspaceSession:
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
    )
    sess.parked_status = "parked"
    sess.parked_event_key = event_key
    sess.parked_at = _now()
    sess.parked_state = {
        "schema_version": 1,
        "yielded": {"tool_name": "ask_user"},
    }
    return sess


async def _build(tmp_path: Path):
    """Return (storage, engine) with a real SQLite-backed storage."""
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "test.sqlite"))
    await provider.initialize()
    storage = provider.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=storage)},
    )
    return storage, engine


async def _seed_parked(storage, session_id: str, event_key: str) -> None:
    sess = _make_parked_session(session_id, event_key)
    await storage.create(sess)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_handle_event_flips_row_and_rearms_lease(tmp_path: Path) -> None:
    """_handle_event flips a parked row to resumable, stamps payload, re-arms lease."""
    storage, engine = await _build(tmp_path)
    ek = "ask_user:sess-1:tc-1"
    await _seed_parked(storage, "sess-1", ek)

    listener = YieldEventListener(bus=None, session_storage=storage, engine=engine)
    await listener._handle_event(_Event(ek, {"answer": "yes"}))

    row = await storage.get("sess-1")
    assert row is not None
    assert row.parked_status == "resumable"
    assert row.parked_state is not None
    assert row.parked_state["resume_event_payload"] == {"answer": "yes"}

    leases = await engine.claim_due("wrk", max_count=10)
    assert "sess-1" in [le.entity_id for le in leases]


async def test_handle_event_no_match_is_noop(tmp_path: Path) -> None:
    """_handle_event with no matching parked row must not raise."""
    storage, engine = await _build(tmp_path)

    listener = YieldEventListener(bus=None, session_storage=storage, engine=engine)
    # Must not raise even when there are zero matching rows.
    await listener._handle_event(_Event("nope:1:2", {}))


async def test_handle_event_idempotent_second_fire(tmp_path: Path) -> None:
    """A second event for the same key does NOT overwrite the already-stamped payload."""
    storage, engine = await _build(tmp_path)
    ek = "ask_user:sess-1:tc-1"
    await _seed_parked(storage, "sess-1", ek)

    listener = YieldEventListener(bus=None, session_storage=storage, engine=engine)
    await listener._handle_event(_Event(ek, {"answer": "first"}))
    await listener._handle_event(_Event(ek, {"answer": "second"}))

    row = await storage.get("sess-1")
    assert row is not None
    # Second fire must not overwrite: the row is already 'resumable' so the
    # guarded flip skips it.
    assert row.parked_state is not None
    assert row.parked_state["resume_event_payload"] == {"answer": "first"}
