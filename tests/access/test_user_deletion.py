"""Deleting a user hands over what they owned (access-control P6 Task 1).

``hand_over`` removes the user from the ``access`` block of every owned and grantable row that names them: sole-owned rows go to the heir (the deleting
admin), co-owned rows keep the others, the user's grants go everywhere, other people's grants on the user's rows stay, a harness's contents follow the
harness, and the triggers that run as the user are disabled. The route runs it between disabling the account and deleting the row.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import primer.model as models
from primer.access.acting import acting_as
from primer.access.lifecycle import GRANTABLE_KINDS, OWNED_KINDS, hand_over, heir_for
from primer.access.person import Person
from primer.model.access import Access, Grantable, Owned
from primer.model.agent import Agent
from primer.model.channel import Channel
from primer.model.collection import Collection
from primer.model.graph import Graph
from primer.model.harness import Harness
from primer.model.model_profile import ModelProfile
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.providers.toolset import Toolset
from primer.model.trigger import DelayedTriggerConfig, Trigger
from primer.model.user import User
from primer.model.workspace import (
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from primer.storage.sqlite import SqliteStorageProvider
from tests.api.conftest import app, client, fake_provider_registry, raw_client  # noqa: F401

NOW = datetime(2026, 10, 31, tzinfo=UTC)
GONE, HEIR, OTHER, PEER = "u-gone", "u-heir", "u-other", "u-peer"


def _ref(uid: str) -> PrincipalRef:
    return PrincipalRef(type="user", id=uid, display=uid, role="user", source="local", user_id=uid)


def _row(model: type, rid: str, access: Access, **extra: Any):
    """One minimal row of ``model`` carrying ``access``."""
    if model is Agent:
        return Agent(id=rid, description="d", model={"profile_id": "p--m"}, access=access, **extra)
    if model is Graph:
        return Graph(id=rid, description="d", nodes=[], edges=[], access=access, **extra)
    if model is Toolset:
        return Toolset(id=rid, provider="internal", access=access, **extra)
    if model is WorkspaceTemplate:
        return WorkspaceTemplate(id=rid, description="d", provider_id="local-1", access=access, **extra)
    if model is Channel:
        return Channel(id=rid, provider_id="cp", provider="slack", external_id=rid, access=access, **extra)
    if model is Workspace:
        return Workspace(
            id=rid, name=rid, template_id="t", provider_id="p", created_at=NOW, phase="running",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"), access=access, **extra,
        )
    if model is Collection:
        return Collection(id=rid, description="d", access=access, **extra)
    if model is Trigger:
        return Trigger(id=rid, slug=rid, name=rid, created_at=NOW, config=DelayedTriggerConfig(fire_at=NOW), access=access, **extra)
    if model is Harness:
        return Harness(id=rid, slug=rid, name=rid, git_url="https://github.com/x/y", created_at=NOW, access=access, **extra)
    raise AssertionError(model)


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "d.sqlite"))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    for uid in (GONE, HEIR, OTHER, PEER):
        await provider.get_storage(User).create(User(id=uid, username=uid, created_at=NOW, role="admin" if uid == HEIR else "user"))
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.mark.parametrize("model", OWNED_KINDS, ids=lambda m: m.__name__)
async def test_sole_owned_rows_go_to_the_deleting_admin(sp, model) -> None:
    store = sp.get_storage(model)
    await store.create(_row(model, "r1", Access(owners=[GONE], grants={PEER: "read"})))

    summary = await hand_over(sp, GONE, HEIR)

    stored = await store.get("r1")
    assert stored.access.owners == [HEIR] and stored.access.grants == {PEER: "read"}, "the peer keeps their grant"
    assert stored.access.rev == 1 and summary[model.__name__] == 1


async def test_co_owned_rows_keep_the_other_owner(sp) -> None:
    await sp.get_storage(Agent).create(_row(Agent, "r1", Access(owners=[GONE, OTHER])))

    await hand_over(sp, GONE, HEIR)

    assert (await sp.get_storage(Agent).get("r1")).access.owners == [OTHER], "the heir is not added"


async def test_the_users_grants_are_removed_everywhere(sp) -> None:
    for model in OWNED_KINDS:
        await sp.get_storage(model).create(_row(model, "r1", Access(owners=[OTHER], grants={GONE: "full", PEER: "read"})))

    await hand_over(sp, GONE, HEIR)

    for model in OWNED_KINDS:
        stored = await sp.get_storage(model).get("r1")
        assert stored.access.owners == [OTHER] and stored.access.grants == {PEER: "read"}, model.__name__
        assert f"u:{GONE}" not in stored.access.members, "no member token names the user any more"


async def test_grantees_of_the_users_rows_keep_their_grants(sp) -> None:
    await sp.get_storage(Collection).create(_row(Collection, "r1", Access(owners=[GONE], grants={PEER: "full", OTHER: "read"}, public=True)))

    await hand_over(sp, GONE, HEIR)

    stored = (await sp.get_storage(Collection).get("r1")).access
    assert stored.owners == [HEIR] and stored.grants == {PEER: "full", OTHER: "read"} and stored.public is True


async def test_grantable_rows_drop_the_use_grant(sp) -> None:
    profiles = sp.get_storage(ModelProfile)
    await profiles.create(ModelProfile(id="p1", description="d", provider_id="p", model_name="m", context_length=1000, access=Access(grants={GONE: "read", PEER: "read"})))
    await sp.get_storage(WorkspaceProvider).create(WorkspaceProvider(
        id="wp1", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/x"), access=Access(grants={GONE: "read"}),
    ))

    summary = await hand_over(sp, GONE, HEIR)

    assert (await profiles.get("p1")).access.grants == {PEER: "read"} and (await profiles.get("p1")).access.owners == []
    assert (await sp.get_storage(WorkspaceProvider).get("wp1")).access.grants == {}
    assert summary["ModelProfile"] == 1 and summary["WorkspaceProvider"] == 1


async def test_harness_contents_follow_the_harness(sp) -> None:
    harness = Harness(id="hns_1", slug="hh", name="h", git_url="https://github.com/x/y", created_at=NOW, access=Access(owners=[GONE], grants={PEER: "read"}))
    await sp.get_storage(Harness).create(harness)
    installed = Agent(id="ag-h", description="d", harness_id="hns_1", model={"profile_id": "p--m"}, access=harness.access)
    await sp.get_storage(Agent).create(installed)

    await hand_over(sp, GONE, HEIR)

    assert (await sp.get_storage(Harness).get("hns_1")).access.owners == [HEIR]
    after = (await sp.get_storage(Agent).get("ag-h")).access
    assert after.owners == [HEIR] and after.grants == {PEER: "read"}, "the fan-out reached the installed row; the direct refusal was not bypassed"


async def test_default_workspace_becomes_an_ordinary_workspace_of_the_heir(sp) -> None:
    await sp.get_storage(Workspace).create(_row(Workspace, "ws-gone", Access(owners=[GONE])))
    user = await sp.get_storage(User).get(GONE)
    await sp.get_storage(User).update(user.model_copy(update={"default_workspace_id": "ws-gone"}))

    await hand_over(sp, GONE, HEIR)

    row = await sp.get_storage(Workspace).get("ws-gone")
    assert row.access.owners == [HEIR] and row.name == f"{GONE} (deleted user)"


async def test_a_concurrent_sharing_edit_is_not_lost(sp) -> None:
    class _Racing:
        """The agent storage with one grant edit landing between the hand-over's read and its first guarded write."""

        def __init__(self, inner) -> None:
            self._inner, self._raced = inner, False

        async def get(self, entity_id, **kw):
            row = await self._inner.get(entity_id, **kw)
            if not self._raced and row is not None:
                self._raced = True
                racing = row.model_copy(update={"access": Access(owners=row.access.owners, grants={**row.access.grants, PEER: "full"}, rev=row.access.rev + 1)})
                await self._inner.update(racing)
            return row

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _SP:
        def get_storage(self, model):
            return _Racing(sp.get_storage(model)) if model is Agent else sp.get_storage(model)

    await sp.get_storage(Agent).create(_row(Agent, "r1", Access(owners=[GONE])))

    await hand_over(_SP(), GONE, HEIR)

    stored = (await sp.get_storage(Agent).get("r1")).access
    assert stored.owners == [HEIR] and stored.grants == {PEER: "full"}, "the concurrent grant survived the retry"


async def test_repeating_the_hand_over_changes_nothing_more(sp) -> None:
    await sp.get_storage(Agent).create(_row(Agent, "r1", Access(owners=[GONE])))
    await hand_over(sp, GONE, HEIR)

    again = await hand_over(sp, GONE, HEIR)

    assert all(count == 0 for count in again.values()), again
    assert (await sp.get_storage(Agent).get("r1")).access.rev == 1


async def test_a_trigger_that_runs_as_the_user_is_disabled_not_run_as_the_heir(sp) -> None:
    triggers = sp.get_storage(Trigger)
    await triggers.create(_row(Trigger, "t1", Access(owners=[GONE]), owner=_ref(GONE), enabled=True))
    await triggers.create(_row(Trigger, "t2", Access(owners=[OTHER]), owner=_ref(OTHER), enabled=True))

    summary = await hand_over(sp, GONE, HEIR)

    t1 = await triggers.get("t1")
    assert t1.enabled is False and t1.owner.id == GONE, "disabled, and the run-as owner is NOT moved to the heir"
    assert (await triggers.get("t2")).enabled is True
    assert summary["Trigger.disabled"] == 1


async def test_with_no_heir_the_sole_owned_row_stays_as_it_is(sp) -> None:
    await sp.get_storage(Agent).create(_row(Agent, "r1", Access(owners=[GONE], grants={PEER: "read"})))

    await hand_over(sp, GONE, None)

    assert (await sp.get_storage(Agent).get("r1")).access.owners == [GONE]


async def test_heir_is_the_deleting_admin_else_the_oldest_admin(sp) -> None:
    await sp.get_storage(User).create(User(id="u-admin-old", username="old", created_at=datetime(2020, 1, 1, tzinfo=UTC), role="admin"))

    assert await heir_for(sp, Person("u-boss", "admin")) == "u-boss"
    assert await heir_for(sp, Person(None, "system")) == "u-admin-old", "auth disabled: the first admin"
    assert await heir_for(sp, Person(OTHER, "user")) == "u-admin-old"


async def test_every_owned_and_grantable_kind_is_listed() -> None:
    owned, grantable = set(), set()
    import importlib
    import pkgutil

    for info in pkgutil.walk_packages(models.__path__, "primer.model."):
        module = importlib.import_module(info.name)
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            if issubclass(cls, Owned) and cls is not Owned:
                owned.add(cls)
            if issubclass(cls, Grantable) and cls is not Grantable:
                grantable.add(cls)

    assert owned == set(OWNED_KINDS), owned ^ set(OWNED_KINDS)
    assert grantable == set(GRANTABLE_KINDS), grantable ^ set(GRANTABLE_KINDS)


# ---- the route --------------------------------------------------------------------------------------------------------------------------------


async def _make_user(client, name: str) -> str:
    made = await client.post("/v1/admin/users", json={"username": name, "password": f"{name}password1", "role": "user"})
    assert made.status_code == 201, made.text
    return made.json()["id"]


async def _admin_id(client) -> str:
    return next(u for u in (await client.get("/v1/admin/users")).json()["items"] if u["username"] == "testuser")["id"]


@pytest.mark.asyncio
async def test_the_route_hands_over_forgets_sign_ins_and_deletes(client, app, monkeypatch) -> None:
    sp = app.state.storage_provider
    bob = await _make_user(client, "bob")
    peer = await _make_user(client, "peer")
    await sp.get_storage(Agent).create(_row(Agent, "r1", Access(owners=[bob], grants={peer: "read"})))
    forgotten: list[str] = []

    import primer.toolset.oauth.store as oauth_store

    async def _spy(storage_provider, user_id):
        forgotten.append(user_id)

    monkeypatch.setattr(oauth_store, "forget_user_sign_ins", _spy)

    r = await client.delete(f"/v1/admin/users/{bob}")

    assert r.status_code == 204, r.text
    assert (await sp.get_storage(Agent).get("r1")).access.owners == [await _admin_id(client)]
    assert (await sp.get_storage(Agent).get("r1")).access.grants == {peer: "read"}
    assert forgotten == [bob] and await sp.get_storage(User).get(bob) is None


@pytest.mark.asyncio
async def test_last_admin_still_refused(client) -> None:
    r = await client.delete(f"/v1/admin/users/{await _admin_id(client)}")

    assert r.status_code == 409 or r.status_code == 400 or r.status_code == 403, r.text
    assert (await client.get("/v1/admin/users")).status_code == 200


@pytest.mark.asyncio
async def test_repeating_the_delete_finishes_a_partial_one(client, app, monkeypatch) -> None:
    sp = app.state.storage_provider
    bob = await _make_user(client, "bob2")
    await sp.get_storage(Agent).create(_row(Agent, "r2", Access(owners=[bob])))
    import primer.access.lifecycle as lifecycle

    real = lifecycle.hand_over

    async def _boom(*a, **k):
        raise RuntimeError("fails part way")

    monkeypatch.setattr(lifecycle, "hand_over", _boom)
    with pytest.raises(RuntimeError):
        await client.delete(f"/v1/admin/users/{bob}")
    left = await sp.get_storage(User).get(bob)
    assert left is not None and left.disabled is True, "a disabled account that still names its leftovers"

    monkeypatch.setattr(lifecycle, "hand_over", real)
    second = await client.delete(f"/v1/admin/users/{bob}")

    assert second.status_code == 204, second.text
    assert (await sp.get_storage(Agent).get("r2")).access.owners == [await _admin_id(client)]


async def test_hand_over_events_name_the_admin(sp) -> None:
    await sp.get_storage(Agent).create(_row(Agent, "r3", Access(owners=[GONE])))
    mark = await sp.get_event_store().max_id()

    with acting_as(Person(HEIR, "admin")):
        await hand_over(sp, GONE, HEIR)

    events = [e for e in await sp.get_event_store().read_after(mark) if e.entity_kind == "agent" and e.entity_id == "r3"]
    assert events and all(e.actor == f"user:{HEIR}" for e in events), [(e.event_type, e.actor) for e in events]
