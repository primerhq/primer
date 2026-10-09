"""A ``parked_session`` subscription wakes the park that created it, not a later park that reuses the raw tool_call_id.

A provider repeats its ``tool_call_id`` across rounds. ``subscribe_to_trigger`` stamps its Subscription with the session and that raw id and parks
with ``resume_metadata["subscription_id"]``; the dispatcher only compared the raw id with the parked state's, so a subscription left behind by an
earlier round (the yield timed out or was skipped and the row stayed) fired onto whatever LATER park reused the id: an approval gate received the
trigger's result as its decision (console review C-033, ticket 01a11f52-9d98). The dispatcher now also requires the park's own yield to carry this
subscription's id; a subscription for any other park is orphaned: skipped (``skipped_session_unparked``) and deleted, nothing published.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import primer.trigger.subscribers.parked_session as ps
from primer.model.trigger import ParkedSessionSubConfig, Subscription
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider

SESSION = "s-gate-id"
RAW = "call_0"


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


@pytest.fixture(autouse=True)
def _no_rank_refusal(monkeypatch):
    async def _none(*_a, **_k):
        return None  # the rank check is orthogonal: these tests are about which park a fire reaches

    monkeypatch.setattr(ps, "refuse_steer_above_fire", _none)


def _sub(sub_id: str) -> Subscription:
    return Subscription(
        id=sub_id, trigger_id="tr-1", created_at=datetime.now(UTC),
        config=ParkedSessionSubConfig(session_id=SESSION, tool_call_id=RAW, parked_at=datetime(2026, 1, 1, tzinfo=UTC)),
    )


def _session(yielded: dict) -> WorkspaceSession:
    return WorkspaceSession(
        id=SESSION, workspace_id="ws-g", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), parked_status="parked", parked_at=datetime.now(UTC), parked_event_key=yielded["event_key"],
        parked_state={"tool_call_id": RAW, "yielded": yielded},
    )


def _approval_gate() -> dict:
    return {
        "tool_name": "_approval", "event_key": f"tool_approval:{SESSION}:{RAW}",
        "resume_metadata": {
            "policy_id": "pol", "approval_type": "required", "gate_id": "a" * 32, "approvers": None,
            "original_call": {"id": RAW, "name": "delete_workspace", "arguments": {}},
        },
    }


def _trigger_park(sub_id: str) -> dict:
    return {
        "tool_name": "subscribe_to_trigger", "event_key": "trigger:tr-1",
        "resume_metadata": {"subscription_id": sub_id, "trigger_id": "tr-1"},
    }


async def _fire(sp, sub: Subscription, bus: _Bus):
    return await ps.ParkedSessionDispatcher().dispatch(
        sub, rendered_payload="", fire_context={"fired": True}, fire_id="f-1",
        deps=DispatchDeps(storage_provider=sp, claim_engine=None, event_bus=bus),
    )


@pytest.mark.asyncio
async def test_an_orphaned_subscription_does_not_wake_a_later_approval_gate():
    sp, bus = _FakeStorageProvider(), _Bus()
    await sp.get_storage(WorkspaceSession).create(_session(_approval_gate()))
    old = _sub("sb-old")
    await sp.get_storage(Subscription).create(old)

    result = await _fire(sp, old, bus)

    assert bus.published == [], "the stale subscription's fire was published onto the approval gate"
    assert (result.ok, result.skipped, result.error_code) == (True, True, "skipped_session_unparked")
    assert await sp.get_storage(Subscription).get("sb-old") is None, "the orphan must delete itself"
    row = await sp.get_storage(WorkspaceSession).get(SESSION)
    assert row.parked_status == "parked", "the approval gate is untouched"


@pytest.mark.asyncio
async def test_a_subscription_for_an_earlier_trigger_park_does_not_wake_the_current_one():
    """Two trigger parks under one raw id: only the subscription the CURRENT park created may wake it."""
    sp, bus = _FakeStorageProvider(), _Bus()
    await sp.get_storage(WorkspaceSession).create(_session(_trigger_park("sb-new")))
    old, new = _sub("sb-old"), _sub("sb-new")
    for sub in (old, new):
        await sp.get_storage(Subscription).create(sub)

    stale = await _fire(sp, old, bus)
    assert bus.published == [] and stale.skipped is True
    assert await sp.get_storage(Subscription).get("sb-old") is None

    fresh = await _fire(sp, new, bus)
    assert fresh.ok and not fresh.skipped
    assert [k for k, _ in bus.published] == ["trigger:tr-1"]
    assert await sp.get_storage(Subscription).get("sb-new") is None, "the one-shot subscription is consumed"


@pytest.mark.asyncio
async def test_a_park_that_carries_no_subscription_id_is_never_woken_by_a_subscription():
    sp, bus = _FakeStorageProvider(), _Bus()
    yielded = _trigger_park("sb-x")
    del yielded["resume_metadata"]["subscription_id"]
    await sp.get_storage(WorkspaceSession).create(_session(yielded))
    sub = _sub("sb-x")
    await sp.get_storage(Subscription).create(sub)

    result = await _fire(sp, sub, bus)

    assert bus.published == [] and result.skipped is True
