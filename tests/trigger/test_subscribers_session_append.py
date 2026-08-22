"""S6 P1: the session_append dispatcher.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 3. The dispatcher
owns only the result envelope; the routing decision lives in
primer.session.steer_delivery.
"""

from __future__ import annotations

from datetime import UTC, datetime

import primer.trigger.subscribers.session_append as sa
from primer.model.trigger import SessionAppendSubConfig, Subscription
from primer.session.steer_delivery import (
    DELIVERED_MISSING,
    DELIVERED_QUEUED,
    DELIVERED_SKIPPED_BUSY,
    DELIVERED_WOKEN,
    SteerDelivery,
)
from primer.trigger.subscribers import DispatchDeps, get_dispatcher
from tests.conftest import _FakeStorageProvider


def _sub(parallelism: str = "queue") -> Subscription:
    return Subscription(
        id="sb-1",
        trigger_id="tr-1",
        config=SessionAppendSubConfig(session_id="s1"),
        parallelism=parallelism,
        created_at=datetime.now(UTC),
    )


def _deps(sp) -> DispatchDeps:
    return DispatchDeps(
        storage_provider=sp,
        claim_engine=object(),
        scheduler=object(),
        workspace_registry=object(),
        event_bus=None,
    )


async def _dispatch(monkeypatch, outcome: str, *, parallelism="queue", deps=None):
    captured: dict = {}

    async def _fake_deliver(**kw):
        captured.update(kw)
        return SteerDelivery(outcome=outcome, session_id="s1")

    monkeypatch.setattr(sa, "deliver_steer", _fake_deliver)
    sp = _FakeStorageProvider()
    res = await sa.SessionAppendDispatcher().dispatch(
        _sub(parallelism),
        rendered_payload="do the thing",
        fire_context={"fire_id": "fire-1"},
        fire_id="fire-1",
        deps=deps if deps is not None else _deps(sp),
    )
    return res, captured


def test_dispatcher_is_registered():
    assert get_dispatcher("session_append") is not None


async def test_wake_reports_the_session_as_the_artefact(monkeypatch):
    res, captured = await _dispatch(monkeypatch, DELIVERED_WOKEN)
    assert res.ok is True
    assert res.skipped is False
    assert res.artefact_id == "s1"
    assert captured["text"] == "do the thing"
    assert captured["parallelism"] == "queue"


async def test_queued_is_a_successful_delivery(monkeypatch):
    res, _ = await _dispatch(monkeypatch, DELIVERED_QUEUED)
    assert res.ok is True
    assert res.skipped is False
    assert res.artefact_id == "s1"


async def test_busy_skip_is_a_non_failing_skip(monkeypatch):
    res, _ = await _dispatch(
        monkeypatch, DELIVERED_SKIPPED_BUSY, parallelism="skip"
    )
    assert res.ok is True
    assert res.skipped is True
    assert res.error_code == "skipped_session_busy"


async def test_missing_target_is_a_non_failing_skip(monkeypatch):
    res, _ = await _dispatch(monkeypatch, DELIVERED_MISSING)
    assert res.ok is True
    assert res.skipped is True
    assert res.error_code == "skipped_session_missing"


async def test_absent_workspace_registry_fails_loudly():
    sp = _FakeStorageProvider()
    res = await sa.SessionAppendDispatcher().dispatch(
        _sub(),
        rendered_payload="x",
        fire_context={},
        fire_id="fire-2",
        deps=DispatchDeps(
            storage_provider=sp, claim_engine=object(), scheduler=object(),
            workspace_registry=None,
        ),
    )
    assert res.ok is False
    assert res.error_code == "dispatch_failed"
