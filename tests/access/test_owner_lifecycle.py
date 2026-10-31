"""Demoted, disabled and deleted owners (access-control P6 Task 2, Decision 6).

A demotion or a disable keeps the rows, grants and sign-ins; ``level_of`` already gives a restricted or disabled person nothing. ``set_access`` validates only
the people an update ADDS, so the remaining owners can still re-share a row that names a restricted or disabled user (dropping them is the owners' call), while
a NEW owner or grantee who is restricted, disabled or missing is still refused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from primer.access.person import Person
from primer.access.policy import level_of
from primer.access.service import AccessUpdate, lookup_users, set_access
from primer.model.access import Access, AccessLevel
from primer.model.common import Identifiable
from primer.model.except_ import ValidationError
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider

NOW = datetime(2026, 10, 31, tzinfo=UTC)


class Gizmo(Identifiable):
    name: str = "g"
    access: Access = Access()


async def _user(sp, uid: str, *, role: str = "user", disabled: bool = False) -> None:
    await sp.get_storage(User).create(User(id=uid, username=uid, password_hash=None, created_at=NOW, role=role, disabled=disabled))


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _reshare(sp, who: str, update: AccessUpdate):
    return await set_access(sp.get_storage(Gizmo), sp.get_storage(User), "g1", Person(who, "user"), update, what="Gizmo 'g1'")


async def test_restricted_owner_reads_nothing_and_regains_on_promotion() -> None:
    access = Access(owners=["u1"], grants={"u2": "full"})

    assert level_of(Person("u1", "restricted"), access) is None
    assert level_of(Person("u1", "user"), access) == AccessLevel.OWNER  # promoted: the row was kept, nothing was rewritten


async def test_coowner_can_reshare_a_row_naming_a_restricted_user(sp) -> None:
    await _user(sp, "u1")
    await _user(sp, "u2", role="restricted")  # a co-owner, demoted
    await _user(sp, "u3")
    await _user(sp, "u4")
    await sp.get_storage(Gizmo).create(Gizmo(id="g1", access=Access(owners=["u1", "u2"], grants={"u3": "read"})))

    view = await _reshare(sp, "u1", AccessUpdate(owners=["u1", "u2"], grants={"u3": "read", "u4": "full"}))

    assert {g.user.id for g in view.grants} == {"u3", "u4"} and [o.id for o in view.owners] == ["u1", "u2"]


async def test_coowner_can_reshare_a_row_naming_a_disabled_user(sp) -> None:
    await _user(sp, "u1")
    await _user(sp, "u2", disabled=True)  # a grantee, disabled
    await _user(sp, "u4")
    await sp.get_storage(Gizmo).create(Gizmo(id="g1", access=Access(owners=["u1"], grants={"u2": "read"})))

    view = await _reshare(sp, "u1", AccessUpdate(owners=["u1"], grants={"u2": "read", "u4": "read"}))

    assert {g.user.id for g in view.grants} == {"u2", "u4"}


async def test_coowner_can_reshare_a_row_naming_a_deleted_user(sp) -> None:
    await _user(sp, "u1")
    await sp.get_storage(Gizmo).create(Gizmo(id="g1", access=Access(owners=["u1", "gone"])))

    view = await _reshare(sp, "u1", AccessUpdate(owners=["u1", "gone"], public=True))

    assert view.public is True and [o.id for o in view.owners] == ["u1", "gone"]


async def test_dropping_the_restricted_user_is_the_owners_call(sp) -> None:
    await _user(sp, "u1")
    await _user(sp, "u2", role="restricted")
    await sp.get_storage(Gizmo).create(Gizmo(id="g1", access=Access(owners=["u1"], grants={"u2": "read"})))

    view = await _reshare(sp, "u1", AccessUpdate(owners=["u1"]))

    assert view.grants == []


@pytest.mark.parametrize("kind", ["restricted", "disabled", "missing"])
async def test_new_grant_to_a_restricted_user_is_still_refused(sp, kind) -> None:
    await _user(sp, "u1")
    if kind == "restricted":
        await _user(sp, "u9", role="restricted")
    elif kind == "disabled":
        await _user(sp, "u9", disabled=True)
    await sp.get_storage(Gizmo).create(Gizmo(id="g1", access=Access(owners=["u1"])))

    with pytest.raises(ValidationError):
        await _reshare(sp, "u1", AccessUpdate(owners=["u1"], grants={"u9": "read"}))
    with pytest.raises(ValidationError):
        await _reshare(sp, "u1", AccessUpdate(owners=["u1", "u9"]))
    assert (await sp.get_storage(Gizmo).get("g1")).access.grants == {}


async def test_grantees_keep_using_a_demoted_owners_public_agent() -> None:
    access = Access(owners=["u1"], grants={"u2": "read"}, public=True)  # u1 is restricted now

    assert level_of(Person("u1", "restricted"), access) is None
    assert level_of(Person("u2", "user"), access) == AccessLevel.READ  # the grant and the public flag are untouched
    assert level_of(Person("u3", "user"), access) == AccessLevel.READ


async def test_trigger_of_a_restricted_owner_is_refused(sp) -> None:
    from primer.model.principal import PrincipalRef
    from primer.model.trigger import AgentFreshSubConfig, Subscription
    from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
    from primer.trigger.reach import refuse_fire_out_of_reach

    await _user(sp, "u1", role="restricted")  # was a user when it saved the subscription
    await sp.get_storage(Workspace).create(Workspace(
        id="ws-1", template_id="t", provider_id="p", created_at=NOW, phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"), access=Access(owners=["u1"]),
    ))
    ref = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local", user_id="u1")
    sub = Subscription(id="sb-1", trigger_id="tr-1", created_at=NOW, owner=ref, config=AgentFreshSubConfig(workspace_id="ws-1", agent_id="ag"))

    reason = await refuse_fire_out_of_reach(sub, sp)

    assert reason is not None, "a subscription of a restricted owner must not fire"


async def test_parked_turn_of_a_deleted_user_does_not_resume(sp) -> None:
    from primer.authz import RunRefused
    from primer.model.principal import PrincipalRef
    from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
    from primer.worker.executor_builders import resolve_run_principal

    ref = PrincipalRef(type="user", id="u-gone", display="gone", role="user", source="local", user_id="u-gone")
    session = WorkspaceSession(
        id="s-1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag"), status=SessionStatus.WAITING, created_at=NOW,
        initiated_by=ref, turn_person=ref,
    )

    with pytest.raises(RunRefused):
        await resolve_run_principal(sp, session)


async def test_lookup_never_lists_restricted_or_disabled_users(sp) -> None:
    await _user(sp, "ann")
    await _user(sp, "ann-restricted", role="restricted")
    await _user(sp, "ann-disabled", disabled=True)

    found = await lookup_users(sp.get_storage(User), Person("ann", "user"), "ann")

    assert [u.id for u in found] == ["ann"]
