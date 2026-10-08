"""S6 P1: the session_append dispatcher.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 3. The dispatcher
owns only the result envelope; the routing decision lives in
primer.session.steer_delivery.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import primer.trigger.subscribers.session_append as sa
from primer.model.storage import OffsetPage
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
    res, captured = await _dispatch(
        monkeypatch, DELIVERED_SKIPPED_BUSY, parallelism="skip"
    )
    assert res.ok is True
    assert res.skipped is True
    assert res.error_code == "skipped_session_busy"
    assert captured["parallelism"] == "skip", "the subscription's parallelism must reach deliver_steer unchanged"


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


# ---- the real routing, not a stubbed deliver_steer (ticket 01a11b55) --------------------------------------------------------------
#
# The stubbed tests above only see what the dispatcher PASSES to deliver_steer. A mutant that hard-coded `parallelism="queue"` there left
# them green while the docs say a skip subscription skips a busy target. These run the real `deliver_steer` over a real busy row: the
# outcome is decided by what is stored, so the subscription's own `parallelism` is the only thing that can make the two cases differ.


async def _busy_target(sp, **row_fields):
    from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

    fields = dict(
        id="s1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), turn_status="running", last_seq=4,
    )
    fields.update(row_fields)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(**fields))


async def _pending_texts(sp) -> list[str]:
    from primer.model.workspace_session import PendingSessionMessage

    page = await sp.get_storage(PendingSessionMessage).list(OffsetPage(offset=0, length=50))
    return [part["text"] for row in page.items if row.session_id == "s1" for part in row.parts]


class _MustNotBeTouched:
    """The wake path's collaborators: using any of them means the dispatcher woke or armed a turn it must have left alone.

    Every access is RECORDED (and raises), so a test can assert on ``touched`` and its message names what was used; the raise alone
    would come back as the dispatcher's ``dispatch_failed`` envelope and read like an unrelated error.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "touched", [])

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"a busy target was woken: {name} was used")


def _real_deps(sp) -> DispatchDeps:
    return DispatchDeps(
        storage_provider=sp, claim_engine=_MustNotBeTouched(), scheduler=_MustNotBeTouched(),
        workspace_registry=_MustNotBeTouched(), event_bus=None,
    )


def _assert_not_woken(deps: DispatchDeps) -> None:
    used = {
        name: collaborator.touched
        for name, collaborator in (("claim_engine", deps.claim_engine), ("scheduler", deps.scheduler), ("workspace_registry", deps.workspace_registry))
        if collaborator.touched
    }
    assert not used, f"a busy target was woken: {used}"


BUSY_ROWS = pytest.mark.parametrize(
    "row_fields",
    [{"turn_status": "running"}, {"turn_status": "claimable"}, {"turn_status": "idle", "parked_status": "parked"}],
    ids=["running", "claimable", "parked"],
)


@BUSY_ROWS
async def test_a_skip_subscription_on_a_busy_target_records_skipped_busy_and_appends_nothing(row_fields):
    sp = _FakeStorageProvider()
    await _busy_target(sp, **row_fields)

    deps = _real_deps(sp)
    res = await sa.SessionAppendDispatcher().dispatch(
        _sub("skip"), rendered_payload="do the thing", fire_context={"fire_id": "fire-1"}, fire_id="fire-1", deps=deps,
    )

    _assert_not_woken(deps)
    assert res.ok is True and res.skipped is True and res.error_code == "skipped_session_busy", res
    assert await _pending_texts(sp) == [], "a skipped steer must not be queued"
    from primer.model.workspace_session import WorkspaceSession

    row = await sp.get_storage(WorkspaceSession).get("s1")
    assert row.last_seq == 4 and row.turn_status == row_fields["turn_status"], "a skipped steer must not touch the session"


@BUSY_ROWS
async def test_a_queue_subscription_on_a_busy_target_queues_the_steer(row_fields):
    sp = _FakeStorageProvider()
    await _busy_target(sp, **row_fields)

    deps = _real_deps(sp)
    res = await sa.SessionAppendDispatcher().dispatch(
        _sub("queue"), rendered_payload="do the thing", fire_context={"fire_id": "fire-1"}, fire_id="fire-1", deps=deps,
    )

    _assert_not_woken(deps)
    assert res.ok is True and res.skipped is False and res.artefact_id == "s1", res
    assert await _pending_texts(sp) == ["do the thing"], "the steer must wait for the open turn, once"
