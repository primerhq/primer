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
