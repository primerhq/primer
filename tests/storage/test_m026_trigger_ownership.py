"""Migration 26: existing triggers get an owner, and every subscription its trigger's owner (access-control Phase 3, Task 4).

A Trigger whose access block is absent or empty is owned by the user behind its run-as owner (a token resolves to its user),
or by the first admin (the enabled admin with the oldest created_at) when that owner is ``system``, missing or no longer
resolves. A subscription with no owner gets its trigger's run-as owner, or the first admin when the trigger has none, AND is
disabled: the row may have been planted by someone else, and the adopted identity would hand it the owner's reach, so the
trigger's owner reviews it and re-enables it (which makes the re-enabler its owner). Rows that already carry access (or an
owner) are left alone, so re-running converges.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from primer.model.access import Access
from primer.model.api_token import ApiToken
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.trigger import AgentFreshSubConfig, DelayedTriggerConfig, Subscription, Trigger
from primer.model.user import User
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m026_trigger_ownership import M026TriggerOwnership
from primer.storage.sqlite import SqliteStorageProvider

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _users(sp, *rows: tuple[str, str, int, bool]) -> None:
    for uid, role, age_days, disabled in rows:
        await sp.get_storage(User).create(
            User(id=uid, username=uid, role=role, disabled=disabled, created_at=_T0 + timedelta(days=age_days))
        )


def _ref(kind: str, rid: str, role: str = "user") -> PrincipalRef:
    return PrincipalRef(type=kind, id=rid, display=rid, role=role, source="local")  # type: ignore[arg-type]


async def _trigger(sp, tid: str, owner: PrincipalRef | None, access: Access | None = None) -> None:
    row = Trigger(id=tid, slug=tid, name=tid, created_at=_T0, owner=owner, config=DelayedTriggerConfig(fire_at=_T0))
    await sp.get_storage(Trigger).create(row if access is None else row.model_copy(update={"access": access}))


async def _sub(sp, sid: str, tid: str, owner: PrincipalRef | None = None) -> None:
    await sp.get_storage(Subscription).create(Subscription(
        id=sid, trigger_id=tid, created_at=_T0, owner=owner, config=AgentFreshSubConfig(workspace_id="ws", agent_id="ag"),
    ))


async def _access(sp, tid: str) -> Access:
    return (await sp.get_storage(Trigger).get(tid)).access


async def test_registered_as_version_26():
    assert MIGRATIONS[25].version == 26 and isinstance(MIGRATIONS[25], M026TriggerOwnership)


async def test_a_trigger_goes_to_the_user_behind_its_run_as_owner(sp):
    await _users(sp, ("u-admin", "admin", 0, False), ("u-alice", "user", 1, False), ("u-bob", "user", 2, False))
    await sp.get_storage(ApiToken).create(ApiToken(id="tok-1", user_id="u-bob", name="t", token_hash="h" * 64, prefix="p" * 8, created_at=_T0))
    await _trigger(sp, "t-user", _ref("user", "u-alice"))
    await _trigger(sp, "t-token", _ref("api_token", "tok-1"))

    await M026TriggerOwnership().apply(sp)

    assert (await _access(sp, "t-user")).owners == ["u-alice"]
    assert (await _access(sp, "t-token")).owners == ["u-bob"]


async def test_system_missing_or_unresolvable_owners_go_to_the_oldest_enabled_admin(sp):
    await _users(sp, ("u-gone-admin", "admin", 0, True), ("u-admin", "admin", 1, False), ("u-off", "user", 2, True))
    await _trigger(sp, "t-system", _ref("system", "system", role="system"))
    await _trigger(sp, "t-none", None)
    await _trigger(sp, "t-deleted", _ref("user", "u-deleted"))
    await _trigger(sp, "t-disabled", _ref("user", "u-off"))
    await _trigger(sp, "t-revoked-token", _ref("api_token", "tok-missing"))

    await M026TriggerOwnership().apply(sp)

    for tid in ("t-system", "t-none", "t-deleted", "t-disabled", "t-revoked-token"):
        access = await _access(sp, tid)
        assert access.owners == ["u-admin"] and access.public is False and access.members == ["u:u-admin"], tid


async def test_a_trigger_that_already_has_access_is_left_alone(sp):
    await _users(sp, ("u-admin", "admin", 0, False))
    await _trigger(sp, "t-mine", _ref("user", "u-admin"), Access(owners=["u-other"]))
    await _trigger(sp, "t-open", None, Access(public=True))

    await M026TriggerOwnership().apply(sp)

    assert (await _access(sp, "t-mine")).owners == ["u-other"]
    assert await _access(sp, "t-open") == Access(public=True)


async def test_ownerless_subscriptions_get_the_triggers_owner_or_the_first_admin(sp):
    await _users(sp, ("u-admin", "admin", 0, False), ("u-alice", "user", 1, False))
    await _trigger(sp, "t-alice", _ref("user", "u-alice"))
    await _trigger(sp, "t-legacy", None)
    await _sub(sp, "s-legacy-on-alice", "t-alice")
    await _sub(sp, "s-legacy-on-legacy", "t-legacy")
    await _sub(sp, "s-owned", "t-alice", _ref("user", "u-other"))

    await M026TriggerOwnership().apply(sp)

    subs = sp.get_storage(Subscription)
    on_alice = await subs.get("s-legacy-on-alice")
    assert on_alice.owner == _ref("user", "u-alice") and on_alice.enabled is False
    on_legacy = await subs.get("s-legacy-on-legacy")
    assert (on_legacy.owner.type, on_legacy.owner.id, on_legacy.owner.user_id) == ("user", "u-admin", "u-admin")
    assert on_legacy.enabled is False
    owned = await subs.get("s-owned")
    assert owned.owner == _ref("user", "u-other") and owned.enabled is True


async def test_without_an_enabled_admin_unresolvable_rows_are_left_for_admins(sp):
    await _users(sp, ("u-user", "user", 0, False))
    await _trigger(sp, "t-none", None)
    await _sub(sp, "s-1", "t-none")

    await M026TriggerOwnership().apply(sp)

    assert await _access(sp, "t-none") == Access()
    assert (await sp.get_storage(Subscription).get("s-1")).owner is None


async def test_rerunning_changes_nothing(sp):
    await _users(sp, ("u-admin", "admin", 0, False), ("u-alice", "user", 1, False))
    await _trigger(sp, "t-1", _ref("user", "u-alice"))
    await _trigger(sp, "t-2", None)
    await _sub(sp, "s-1", "t-2")

    await M026TriggerOwnership().apply(sp)
    first = ({t: (await _access(sp, t)) for t in ("t-1", "t-2")}, (await sp.get_storage(Subscription).get("s-1")).owner)
    await M026TriggerOwnership().apply(sp)
    second = ({t: (await _access(sp, t)) for t in ("t-1", "t-2")}, (await sp.get_storage(Subscription).get("s-1")).owner)
    assert first == second


async def test_an_adopted_subscription_stays_disabled_on_a_rerun_and_a_re_enable_makes_the_enabler_its_owner(sp):
    from primer.access.person import Person
    from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
    from primer.trigger.service import ServiceDeps, update_subscription

    await _users(sp, ("u-admin", "admin", 0, False), ("u-alice", "user", 1, False))
    await sp.get_storage(Workspace).create(Workspace(
        id="ws", template_id="t", provider_id="p", created_at=_T0, runtime_meta=WorkspaceRuntimeMeta(url="ws://ws", token="t"),
        access=Access(owners=["u-alice"]),
    ))
    await _trigger(sp, "t-alice", _ref("user", "u-alice"))
    await _sub(sp, "s-legacy", "t-alice")

    await M026TriggerOwnership().apply(sp)
    await M026TriggerOwnership().apply(sp)
    subs = sp.get_storage(Subscription)
    assert (await subs.get("s-legacy")).enabled is False

    # The trigger's owner reviews it and re-enables it through the normal path: the re-enabler is now its owner.
    alice = _ref("user", "u-alice").model_copy(update={"user_id": "u-alice"})
    reenabled = await update_subscription(
        trigger_id="t-alice", subscription_id="s-legacy", enabled=True,
        owner=alice, person=Person("u-alice", "user"), deps=ServiceDeps(storage_provider=sp),
    )
    assert reenabled.enabled is True and reenabled.owner == alice
    await M026TriggerOwnership().apply(sp)
    assert (await subs.get("s-legacy")).enabled is True


# -- golden characterization (PERF-T16) ---------------------------------------------------------------------------------------------------

from typing import Any  # noqa: E402

from primer.storage.migrations._rows import read_all  # noqa: E402
from primer.storage.migrations.m026_trigger_ownership import (  # noqa: E402
    ApiToken as TokenView,
    Subscription as SubView,
    Trigger as TriggerView,
    User as UserView,
    _owned_by,
    _unset,
)
from tests.storage._migration_golden import assert_golden, insert_raw  # noqa: E402


async def _old_apply(sp) -> None:
    """Migration 26 as it was before PERF-T16, frozen: read every trigger and subscription, ``update`` each one that needs an owner."""
    users = {u.id: u for u in await read_all(sp.get_storage(UserView))}
    admins = [u for u in users.values() if u.role == "admin" and not u.disabled and u.created_at is not None]
    first_admin = min(admins, key=lambda u: u.created_at) if admins else None
    tokens = sp.get_storage(TokenView)

    async def user_of(owner: dict[str, Any] | None) -> str | None:
        if not owner:
            return None
        if owner.get("type") == "user":
            user_id = owner.get("user_id") or owner.get("id")
        elif owner.get("type") == "api_token":
            token = await tokens.get(owner.get("id") or "")
            user_id = token.user_id if token is not None else None
        else:
            return None
        user = users.get(user_id) if user_id else None
        return user_id if user is not None and not user.disabled else None

    triggers = sp.get_storage(TriggerView)
    rows = await read_all(triggers)
    for row in rows:
        if not _unset(row.access):
            continue
        owner_id = await user_of(row.owner) or (first_admin.id if first_admin is not None else None)
        if owner_id is None:
            continue
        await triggers.update(row.model_copy(update={"access": _owned_by(owner_id)}))

    run_as = {row.id: row.owner for row in rows}
    admin_ref = (
        {"type": "user", "id": first_admin.id, "display": first_admin.username or first_admin.id, "role": "admin",
         "source": "local", "user_id": first_admin.id}
        if first_admin is not None else None
    )
    subs = sp.get_storage(SubView)
    for sub in await read_all(subs):
        if sub.owner is not None:
            continue
        owner = run_as.get(sub.trigger_id) or admin_ref
        if owner is None:
            continue
        await subs.update(sub.model_copy(update={"owner": owner, "enabled": False}))


def _golden_seed(with_admin: bool):
    async def seed(sp) -> None:
        users = {
            "u1": {"username": "alice", "role": "user", "created_at": "2025-03-01T00:00:00Z"},
            "u-off": {"role": "user", "disabled": True, "created_at": "2025-03-02T00:00:00Z"},
        }
        if with_admin:
            users |= {
                "admin-old": {"role": "admin", "created_at": "2025-01-01T00:00:00Z"},
                "admin-new": {"username": "newer", "role": "admin", "created_at": "2025-06-01T00:00:00Z"},
                "admin-off": {"role": "admin", "disabled": True, "created_at": "2024-01-01T00:00:00Z"},
            }
        await insert_raw(sp, UserView, users)
        await insert_raw(sp, TokenView, {"tok1": {"user_id": "u1"}, "tok-orphan": {"user_id": "nobody"}})
        alice = {"type": "user", "id": "u1", "display": "alice", "role": "user", "source": "local", "user_id": "u1"}
        await insert_raw(sp, TriggerView, {
            "t-user": {"slug": "a", "owner": alice, "n": 2.5},
            "t-token": {"owner": {"type": "api_token", "id": "tok1"}},
            "t-orphan-token": {"owner": {"type": "api_token", "id": "tok-orphan"}},
            "t-system": {"owner": {"type": "system", "id": "system"}},
            "t-none": {"slug": "no-owner-key"},
            "t-null": {"owner": None, "access": None},
            "t-empty": {"owner": alice, "access": {}},
            "t-owned": {"owner": alice, "access": {"owners": ["u1"], "grants": {}, "public": False}},
            "t-public": {"access": {"owners": [], "public": True}},
            "t-off": {"owner": {"type": "user", "id": "u-off"}},
        })
        await insert_raw(sp, SubView, {
            "s-user": {"trigger_id": "t-user", "config": {"kind": "agent_fresh", "x": [1, {"y": None}]}},
            "s-none": {"trigger_id": "t-none", "enabled": True},
            "s-null": {"trigger_id": "t-system", "owner": None},
            "s-owned": {"trigger_id": "t-user", "owner": {"type": "user", "id": "u1"}, "enabled": True},
            "s-empty-owner": {"trigger_id": "t-user", "owner": {}},
            "s-gone": {"trigger_id": "t-gone"},
            "s-token": {"trigger_id": "t-token", "enabled": False},
            "s-off": {"trigger_id": "t-off", "big": 12345678901234567890},
        })
    return seed


@pytest.mark.parametrize("with_admin", [True, False])
async def test_golden_rows_match_the_old_implementation_and_a_second_run_changes_nothing(tmp_path, with_admin) -> None:
    views = (TriggerView, SubView, UserView, TokenView)
    await assert_golden(tmp_path, _golden_seed(with_admin), views, _old_apply, M026TriggerOwnership().apply)
