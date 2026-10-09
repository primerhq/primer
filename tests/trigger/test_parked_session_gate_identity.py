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
from primer.model.yield_ import WAKE_ENTRY_KEY
from primer.session.yields import flip_sessions_parked_on
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
    assert bus.published[0][1][WAKE_ENTRY_KEY] == "sb-new", "the fire names the subscription it answers: a redelivered copy cannot decide a later park"
    assert await sp.get_storage(Subscription).get("sb-new") is None, "the one-shot subscription is consumed"


class _FlippingBus(_Bus):
    """A bus that, like the listener, flips whatever is parked on the key it publishes."""

    def __init__(self, sessions) -> None:
        super().__init__()
        self._sessions = sessions

    async def publish(self, key: str, payload: dict) -> None:
        await super().publish(key, payload)
        await flip_sessions_parked_on(key, payload, session_storage=self._sessions, engine=None)


@pytest.mark.asyncio
async def test_a_park_in_flight_on_the_old_shared_trigger_key_is_still_woken_by_the_dispatcher():
    """Back-compat of the session-scoped trigger key (ticket 01a1208d, #702 review N-g): a park written by an earlier build stored ``trigger:tr-1``; the dispatcher
    publishes the key the PARK stored, so the subscription still wakes it, through the real flip, and nothing needs a migration."""
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(_session(_trigger_park("sb-old")))
    sub = _sub("sb-old")
    await sp.get_storage(Subscription).create(sub)
    bus = _FlippingBus(sessions)

    result = await _fire(sp, sub, bus)

    assert (result.ok, result.skipped) == (True, False)
    assert [k for k, _ in bus.published] == ["trigger:tr-1"], "the key the park stored, not a freshly built one"
    row = await sessions.get(SESSION)
    assert row.parked_status == "resumable" and row.parked_state["resume_event_payload"]["fire_context"] == {"fired": True}


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


# ---- a graph park: the fire is published onto the PRIMARY's key -------------------------------------------------------------------------------------
# ``respond_to_yield`` answers the park's primary (``yielded``, the first pending entry projected on top). A subscription whose entry is a SIBLING of
# the primary carries its id too, so the "does an entry carry my subscription" check passes, and the fire would be published onto the primary's key:
# another node's approval gate, decided by a trigger result.

PRIMARY_KEY = f"tool_approval:{SESSION}:B:{RAW}"
SIBLING_KEY = "trigger:tr-1"


def _graph_session(*, primary: str) -> WorkspaceSession:
    """Node B parked on an approval gate and node A on ``subscribe_to_trigger`` (sub ``sb-A``), both under the raw id ``call_0``; ``primary`` is the key on top."""
    b = {
        "node_id": "B", "tool_call_id": RAW, "tool_name": "_approval", "event_key": PRIMARY_KEY,
        "resume_metadata": {"gate_id": "b" * 32, "approvers": None, "original_call": {"id": RAW, "name": "delete_workspace", "arguments": {}}},
    }
    a = {
        "node_id": "A", "tool_call_id": RAW, "tool_name": "subscribe_to_trigger", "event_key": SIBLING_KEY,
        "resume_metadata": {"subscription_id": "sb-A", "trigger_id": "tr-1"},
    }
    top = b if primary == PRIMARY_KEY else a
    return WorkspaceSession(
        id=SESSION, workspace_id="ws-g", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), parked_status="parked", parked_at=datetime.now(UTC), parked_event_key=primary,
        parked_event_keys=[PRIMARY_KEY, SIBLING_KEY],
        parked_state={
            "tool_call_id": RAW,
            "yielded": {"tool_name": top["tool_name"], "event_key": primary, "resume_metadata": dict(top["resume_metadata"]), "event_keys": [PRIMARY_KEY, SIBLING_KEY]},
            "graph_checkpoint": {"pending_toolcalls": [], "pending_agent_yields": [top, a if top is b else b], "pending_dispatch": []},
        },
    )


@pytest.mark.asyncio
async def test_a_sibling_nodes_subscription_does_not_reach_the_primary_approval():
    sp, bus = _FakeStorageProvider(), _Bus()
    await sp.get_storage(WorkspaceSession).create(_graph_session(primary=PRIMARY_KEY))
    sub = _sub("sb-A")
    await sp.get_storage(Subscription).create(sub)

    result = await _fire(sp, sub, bus)

    assert bus.published == [], "the trigger result was published onto sibling B's approval gate"
    assert (result.ok, result.skipped) == (True, True)
    assert await sp.get_storage(Subscription).get("sb-A") is not None, "the subscription is pending, only not primary: it must not be deleted"
    assert (await sp.get_storage(WorkspaceSession).get(SESSION)).parked_status == "parked"


@pytest.mark.asyncio
async def test_the_subscription_fires_once_its_entry_is_the_primary():
    """The control: the same graph park with the trigger node on top. The fire reaches the park the subscription created."""
    sp, bus = _FakeStorageProvider(), _Bus()
    await sp.get_storage(WorkspaceSession).create(_graph_session(primary=SIBLING_KEY))
    sub = _sub("sb-A")
    await sp.get_storage(Subscription).create(sub)

    result = await _fire(sp, sub, bus)

    assert result.ok and not result.skipped
    assert [k for k, _ in bus.published if k.startswith(("trigger:", "tool_approval:"))] == [SIBLING_KEY]
    assert await sp.get_storage(Subscription).get("sb-A") is None
