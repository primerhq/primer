"""A fresh-session fire whose subscription owner is gone is refused up front (review of #853, B1).

The fired run is the subscription owner's, and a run whose creator is deleted, disabled or holds a revoked or expired token is
refused at its first turn (``resolve_run_principal``, lead ruling 2 of the G7 fixes). ``principal_for_fire`` fails closed to
``user`` for such an owner, so the ``agent_fresh_session`` and ``graph_fresh_session`` dispatchers used to make a session that
then failed unseen while the fire reported ``ok``. They now answer ``ok=False``, ``error_code="owner_unresolvable"``, and make
no session, so the failure shows on the subscription.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import primer.trigger.subscribers.agent_fresh_session as afs
import primer.trigger.subscribers.graph_fresh_session as gfs
from primer.model.api_token import ApiToken
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.trigger import AgentFreshSubConfig, GraphFreshSubConfig, Subscription
from primer.model.user import User
from primer.model.workspace_session import WorkspaceSession
from primer.trigger.subscribers import DispatchDeps

NOW = datetime.now(UTC)
USER = PrincipalRef(type="user", id="u-ada", display="ada", role="admin", source="local")
TOKEN = PrincipalRef(type="api_token", id="tok-1", display="ci", role="admin", source="internal")


def _sub(kind: str, owner: PrincipalRef | None) -> Subscription:
    config = (
        AgentFreshSubConfig(workspace_id="ws-1", agent_id="ag-1") if kind == "agent"
        else GraphFreshSubConfig(workspace_id="ws-1", graph_id="g-1")
    )
    return Subscription.model_validate({
        "id": "sb-1", "trigger_id": "tr-1", "config": config.model_dump(mode="json"), "parallelism": "queue",
        "created_at": NOW.isoformat(), "owner": owner.model_dump(mode="json") if owner is not None else None,
    })


async def _fire(sp, kind: str, owner: PrincipalRef | None):
    dispatcher = afs.AgentFreshSessionDispatcher() if kind == "agent" else gfs.GraphFreshSessionDispatcher()
    return await dispatcher.dispatch(
        _sub(kind, owner), rendered_payload="{}", fire_context={}, fire_id="fire-1",
        # No registry: a fire that gets past the owner check stops at "dispatch_failed", which tells the two apart.
        deps=DispatchDeps(storage_provider=sp, claim_engine=None, scheduler=None, workspace_registry=None, event_bus=None),
    )


async def _sessions(sp) -> list:
    return list((await sp.get_storage(WorkspaceSession).find(None, OffsetPage(offset=0, length=10))).items)


@pytest.mark.parametrize("kind", ["agent", "graph"])
@pytest.mark.parametrize(
    ("owner", "seed"),
    [
        pytest.param(USER, "none", id="a deleted user"),
        pytest.param(USER, "disabled", id="a disabled user"),
        pytest.param(TOKEN, "revoked", id="a revoked token"),
        pytest.param(TOKEN, "expired", id="an expired token"),
    ],
)
async def test_a_fire_whose_subscription_owner_is_gone_is_refused_and_makes_no_session(fake_storage_provider, kind, owner, seed):
    sp = fake_storage_provider
    if seed == "disabled":
        await sp.get_storage(User).create(User(id="u-ada", username="ada", created_at=NOW, role="admin", disabled=True))
    if seed in ("revoked", "expired"):
        await sp.get_storage(User).create(User(id="u-ada", username="ada", created_at=NOW, role="admin"))
        await sp.get_storage(ApiToken).create(ApiToken(
            id="tok-1", user_id="u-ada", name="ci", token_hash="0" * 64, prefix="prmr_abc", created_at=NOW,
            **({"revoked_at": NOW - timedelta(minutes=1)} if seed == "revoked" else {"expires_at": NOW - timedelta(minutes=1)}),
        ))

    res = await _fire(sp, kind, owner)

    assert (res.ok, res.error_code) == (False, "owner_unresolvable"), res
    assert "no session" in (res.error_message or "")
    assert await _sessions(sp) == []


@pytest.mark.parametrize("kind", ["agent", "graph"])
@pytest.mark.parametrize("owner", [USER, None], ids=["a current user", "a row saved before owners were recorded"])
async def test_a_fire_whose_owner_resolves_or_was_never_recorded_is_not_refused_for_it(fake_storage_provider, kind, owner):
    sp = fake_storage_provider
    await sp.get_storage(User).create(User(id="u-ada", username="ada", created_at=NOW, role="admin"))

    res = await _fire(sp, kind, owner)

    assert res.error_code == "dispatch_failed", res
