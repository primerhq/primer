"""S6 P1: one steer-delivery path for every machine entrypoint.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 3 (session_append)
and section 5 (thread reply). Both routes go through this helper so the
S1 routing rule is applied in exactly one place.
"""

from __future__ import annotations

from datetime import UTC, datetime

import primer.session.steer_delivery as sd
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.steer_delivery import (
    DELIVERED_MISSING,
    DELIVERED_QUEUED,
    DELIVERED_SKIPPED_BUSY,
    DELIVERED_WOKEN,
    deliver_steer,
)
from tests.conftest import _FakeStorageProvider


def _row(**kw) -> WorkspaceSession:
    base = {
        "id": "s1",
        "workspace_id": "w1",
        "binding": AgentSessionBinding(agent_id="ag1"),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
        "turn_status": "idle",
    }
    base.update(kw)
    return WorkspaceSession(**base)


async def _seed(sp, row):
    await sp.get_storage(WorkspaceSession).create(row)
    return row


def _kw(sp):
    return dict(
        storage_provider=sp,
        scheduler=object(),
        claim_engine=object(),
        workspace_registry=object(),
    )


async def test_idle_session_is_woken(monkeypatch):
    sp = _FakeStorageProvider()
    await _seed(sp, _row())
    woken = []

    async def _fake_wake(**kw):
        woken.append(kw["instruction"])

    monkeypatch.setattr(sd, "wake_session", _fake_wake)
    out = await deliver_steer(
        session_id="s1", text="go", parallelism="queue", **_kw(sp)
    )
    assert out.outcome == DELIVERED_WOKEN
    assert out.session_id == "s1"
    assert woken == ["go"]


async def test_busy_session_queues_under_queue_parallelism(monkeypatch):
    sp = _FakeStorageProvider()
    await _seed(sp, _row(turn_status="running"))
    queued = []

    async def _fake_store(**kw):
        queued.append(kw["text"])

    monkeypatch.setattr(sd, "store_pending_steer", _fake_store)
    out = await deliver_steer(
        session_id="s1", text="later", parallelism="queue", **_kw(sp)
    )
    assert out.outcome == DELIVERED_QUEUED
    assert queued == ["later"]


async def test_busy_session_is_dropped_under_skip_parallelism(monkeypatch):
    sp = _FakeStorageProvider()
    await _seed(sp, _row(turn_status="running"))

    async def _must_not_run(**_kw):  # pragma: no cover - must not be called
        raise AssertionError("skip parallelism must not queue")

    monkeypatch.setattr(sd, "store_pending_steer", _must_not_run)
    monkeypatch.setattr(sd, "wake_session", _must_not_run)
    out = await deliver_steer(
        session_id="s1", text="drop me", parallelism="skip", **_kw(sp)
    )
    assert out.outcome == DELIVERED_SKIPPED_BUSY


async def test_missing_session_reports_missing():
    sp = _FakeStorageProvider()
    out = await deliver_steer(
        session_id="nope", text="x", parallelism="queue", **_kw(sp)
    )
    assert out.outcome == DELIVERED_MISSING


async def test_unusable_session_reports_missing(monkeypatch):
    """A row that cannot take a message is 'missing' to the caller.

    wake_session raises ConflictError for a non-restartable ended_reason
    (workspace_lost / force_deleted). Letting that escape would drop the
    inbound channel message with a traceback instead of remapping the
    thread to a fresh session.
    """
    from primer.model.except_ import ConflictError

    sp = _FakeStorageProvider()
    await _seed(sp, _row())

    async def _boom(**_kw):
        raise ConflictError("session is not restartable")

    monkeypatch.setattr(sd, "wake_session", _boom)
    out = await deliver_steer(
        session_id="s1", text="x", parallelism="queue", **_kw(sp)
    )
    assert out.outcome == DELIVERED_MISSING
