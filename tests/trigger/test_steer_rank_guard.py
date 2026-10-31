"""A fire cannot steer a session that outranks it (lead review of #491, security review A-20).

``session_append`` and ``parked_session`` subscriptions put text into an EXISTING session, which then runs at that session's own
``initiated_by``. So a user who could point a subscription at an admin's session would drive an admin-ranked run. Each
dispatcher now compares the target session's initiator with :func:`primer.trigger.owner.principal_for_fire` and refuses (a
failed fire, ``error_code="steer_outranks_fire"``, nothing delivered) when the session outranks the fire.

Before that rank question, the dispatcher asks the access question (access-control P2 T6): the owner recorded on the row must
hold full on the target's workspace (:func:`primer.trigger.reach.refuse_fire_out_of_reach`), else the fire is refused as
``target_out_of_reach`` and the rank guard is never reached. A ``restricted`` owner holds no level on anything, so a
restricted fire is refused by that check no matter who started the target.
"""

from __future__ import annotations

from datetime import datetime, timezone

import primer.trigger.subscribers.parked_session as ps
import primer.trigger.subscribers.session_append as sa
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.trigger import ParkedSessionSubConfig, SessionAppendSubConfig, Subscription, Trigger
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.steer_delivery import DELIVERED_WOKEN, SteerDelivery
from primer.trigger.subscribers import DispatchDeps

NOW = datetime.now(timezone.utc)


def _ref(uid: str, role: str) -> PrincipalRef:
    return PrincipalRef(type="user", id=uid, display=uid, role=role, source="local")


_UNSET = object()


async def _seed(sp, *, session_by: PrincipalRef | None, owner: PrincipalRef | None, config, trigger_owner=_UNSET) -> Subscription:
    """``owner`` owns the subscription and, unless ``trigger_owner`` says otherwise, the trigger too."""
    trigger_owner = owner if trigger_owner is _UNSET else trigger_owner
    for uid, role in (("u-admin", "admin"), ("u-plain", "user"), ("u-restricted", "restricted")):
        await sp.get_storage(User).create(User(id=uid, username=uid, created_at=NOW, role=role))
    # The target's workspace, owned by the subscription's owner so the access check (P2 T6) passes and the rank guard is
    # the question these tests answer. A restricted owner owns it too: the check still refuses, because a restricted person
    # holds no level on anything.
    await sp.get_storage(Workspace).create(Workspace(
        id="ws-1", template_id="local-default", provider_id="local", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://ws-1", token="t"),
        access=Access(owners=[owner.id] if owner is not None else []),
    ))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="se-target", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.WAITING, turn_status="idle", parked_status="parked",
        parked_event_key="subscribe_to_trigger:tc-1",
        parked_state={"tool_call_id": "tc-1", "yielded": {
            "tool_name": "subscribe_to_trigger", "event_key": "subscribe_to_trigger:tc-1",
            "resume_metadata": {"tool_call_id": "tc-1", "subscription_id": "sb-1"},
        }},
        created_at=NOW, initiated_by=session_by,
    ))
    await sp.get_storage(Trigger).create(Trigger.model_validate({
        "id": "tr-1", "slug": "steer-trigger", "name": "t", "created_at": NOW.isoformat(),
        "config": {"kind": "delayed", "fire_at": NOW.isoformat()},
        "owner": trigger_owner.model_dump(mode="json") if trigger_owner else None,
    }))
    sub = Subscription.model_validate({
        "id": "sb-1", "trigger_id": "tr-1", "config": config.model_dump(mode="json"), "parallelism": "queue",
        "created_at": NOW.isoformat(), "owner": owner.model_dump(mode="json") if owner else None,
    })
    await sp.get_storage(Subscription).create(sub)
    return sub


def _deps(sp, event_bus=None) -> DispatchDeps:
    return DispatchDeps(
        storage_provider=sp, claim_engine=object(), scheduler=object(), workspace_registry=object(), event_bus=event_bus,
    )


async def _append(monkeypatch, sp, sub):
    delivered: list = []

    async def _fake_deliver(**kw):
        delivered.append(kw)
        return SteerDelivery(outcome=DELIVERED_WOKEN, session_id=kw["session_id"])

    monkeypatch.setattr(sa, "deliver_steer", _fake_deliver)
    res = await sa.SessionAppendDispatcher().dispatch(
        sub, rendered_payload="call system__create_toolset", fire_context={}, fire_id="fire-1", deps=_deps(sp),
    )
    return res, delivered


async def _wake(monkeypatch, sp, sub):
    woken: list = []

    async def _fake_respond(**kw):
        woken.append(kw)

    monkeypatch.setattr(ps, "respond_to_yield", _fake_respond)
    res = await ps.ParkedSessionDispatcher().dispatch(
        sub, rendered_payload="{}", fire_context={}, fire_id="fire-1", deps=_deps(sp, event_bus=object()),
    )
    return res, woken


APPEND = SessionAppendSubConfig(session_id="se-target")
PARKED = ParkedSessionSubConfig(session_id="se-target", tool_call_id="tc-1", parked_at=NOW)


async def test_a_user_fire_cannot_append_to_an_admin_session(monkeypatch, fake_storage_provider):
    sub = await _seed(fake_storage_provider, session_by=_ref("u-admin", "admin"), owner=_ref("u-plain", "user"), config=APPEND)

    res, delivered = await _append(monkeypatch, fake_storage_provider, sub)

    assert res.ok is False and res.error_code == "steer_outranks_fire", res
    assert "outranks" in (res.error_message or "")
    assert delivered == []


async def test_a_legacy_ownerless_fire_cannot_append_to_a_system_session(monkeypatch, fake_storage_provider):
    sub = await _seed(fake_storage_provider, session_by=PrincipalRef.system(), owner=None, config=APPEND)

    res, delivered = await _append(monkeypatch, fake_storage_provider, sub)

    # Refused before the steer rank is asked: an ownerless subscription never fires (access-control P3 Task 4).
    assert res.ok is False and res.error_code == "target_out_of_reach", res
    assert delivered == []


async def test_an_admin_fire_still_appends_to_an_admin_session(monkeypatch, fake_storage_provider):
    sub = await _seed(fake_storage_provider, session_by=_ref("u-admin", "admin"), owner=_ref("u-admin", "admin"), config=APPEND)

    res, delivered = await _append(monkeypatch, fake_storage_provider, sub)

    assert res.ok is True, res
    assert len(delivered) == 1


async def test_a_user_fire_still_appends_to_a_user_session(monkeypatch, fake_storage_provider):
    sub = await _seed(fake_storage_provider, session_by=_ref("u-plain", "user"), owner=_ref("u-plain", "user"), config=APPEND)

    res, delivered = await _append(monkeypatch, fake_storage_provider, sub)

    assert res.ok is True, res
    assert len(delivered) == 1


async def test_a_user_fire_cannot_wake_a_parked_admin_session(monkeypatch, fake_storage_provider):
    sub = await _seed(fake_storage_provider, session_by=_ref("u-admin", "admin"), owner=_ref("u-plain", "user"), config=PARKED)

    res, woken = await _wake(monkeypatch, fake_storage_provider, sub)

    assert res.ok is False and res.error_code == "steer_outranks_fire", res
    assert woken == []


async def test_an_admin_fire_still_wakes_a_parked_admin_session(monkeypatch, fake_storage_provider):
    sub = await _seed(fake_storage_provider, session_by=_ref("u-admin", "admin"), owner=_ref("u-admin", "admin"), config=PARKED)

    res, woken = await _wake(monkeypatch, fake_storage_provider, sub)

    assert res.ok is True, res
    assert len(woken) == 1


async def test_an_admin_fire_appends_to_a_system_session_and_a_user_fire_does_not(monkeypatch, fake_storage_provider):
    """Re-pinned (review of #853, round 2): ``system`` used to outrank ``admin`` here too. ``may_drive``, the one rank rule of every path that drives
    a session, now ranks an admin with the system principal: an admin clears every role floor, so steering a system session gains nothing, and a
    person could not otherwise act on a session recorded as system at all. A user-ranked fire is still refused."""
    sub = await _seed(fake_storage_provider, session_by=PrincipalRef.system(), owner=_ref("u-admin", "admin"), config=APPEND)

    res, delivered = await _append(monkeypatch, fake_storage_provider, sub)

    assert res.ok is True, res
    assert len(delivered) == 1


async def test_a_user_fire_cannot_append_to_a_system_session(monkeypatch, fake_storage_provider):
    sub = await _seed(fake_storage_provider, session_by=PrincipalRef.system(), owner=_ref("u-plain", "user"), config=APPEND)

    res, delivered = await _append(monkeypatch, fake_storage_provider, sub)

    assert res.ok is False and res.error_code == "steer_outranks_fire", res
    assert delivered == []


async def test_an_admin_subscription_on_a_user_trigger_cannot_wake_a_parked_admin_session(monkeypatch, fake_storage_provider):
    sub = await _seed(
        fake_storage_provider, session_by=_ref("u-admin", "admin"), owner=_ref("u-admin", "admin"), config=PARKED,
        trigger_owner=_ref("u-plain", "user"),
    )

    res, woken = await _wake(monkeypatch, fake_storage_provider, sub)

    assert res.ok is False and res.error_code == "steer_outranks_fire", res
    assert woken == []


async def test_a_restricted_fire_is_refused_by_the_access_check_before_the_rank_guard(monkeypatch, fake_storage_provider):
    """A restricted owner holds no level on any workspace (not even one it owns), so the access check refuses the fire and the
    rank guard is never reached (access-control P2 T6)."""
    restricted = _ref("u-restricted", "restricted")
    sub = await _seed(fake_storage_provider, session_by=None, owner=restricted, config=APPEND)

    res, delivered = await _append(monkeypatch, fake_storage_provider, sub)

    assert res.ok is False and res.error_code == "target_out_of_reach", res
    assert delivered == []


async def test_the_stored_refusal_reason_names_ranks_not_user_ids(monkeypatch, fake_storage_provider):
    sub = await _seed(fake_storage_provider, session_by=_ref("u-admin", "admin"), owner=_ref("u-plain", "user"), config=APPEND)

    res, _ = await _append(monkeypatch, fake_storage_provider, sub)

    assert res.error_code == "steer_outranks_fire"
    assert "u-admin" not in res.error_message and "u-plain" not in res.error_message
    assert "admin" in res.error_message and "user" in res.error_message
