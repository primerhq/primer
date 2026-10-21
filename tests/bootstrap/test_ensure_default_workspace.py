"""The default workspace is ensure-seeded, never re-created (pivot P2 task 4).

A fresh install creates ``prime`` and records it in system state. An install that predates the record keeps the id it
has (``primer``): the ensure pass adopts that row, renames it for display and records it, and never creates a second
default workspace. The id is recorded BEFORE the row is created, so there is no instant at which the row exists and the
delete guard cannot name it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.bootstrap.defaults import (
    RESERVED_LOCAL_WORKSPACE_TEMPLATE,
    RESERVED_WORKSPACE_TEMPLATES,
)
from primer.bootstrap.seed import ensure_default_workspace
from primer.model.except_ import ConflictError
from primer.model.storage import OffsetPage
from primer.model.workspace import (
    Workspace,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from primer.model.workspace_refusal import ReservedWorkspaceError


class _LiveWorkspace:
    """Stand-in for the live handle ``materialise`` returns.

    ``runtime_meta`` is a real ``WorkspaceRuntimeMeta`` because the row
    field is required and validated; an empty dict would fail at create.
    """

    def __init__(self, workspace_id: str | None) -> None:
        self.id = workspace_id
        self.runtime_meta = WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959", token="stub-token",
        )


class _Registry:
    def __init__(self) -> None:
        self.calls = 0
        self.ids: list[str | None] = []

    async def materialise(self, *, template, overrides=None, workspace_id=None):
        self.calls += 1
        self.ids.append(workspace_id)
        return _LiveWorkspace(workspace_id)


async def _seed_template(sp, template_id: str = RESERVED_LOCAL_WORKSPACE_TEMPLATE, **overrides):
    await sp.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate(
            **{**RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE], "id": template_id, **overrides}
        )
    )


def _row(workspace_id: str, name: str | None = None) -> Workspace:
    return Workspace(
        id=workspace_id,
        name=name,
        template_id="local-default",
        provider_id="local",
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token"),
    )


async def _recorded(sp) -> str | None:
    return (await sp.get_system_state()).default_workspace_id


async def _ids(sp) -> list[str]:
    page = await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=50))
    return sorted(w.id for w in page.items)


# ---- a fresh install ---------------------------------------------------------------------------------------------------------


async def test_a_fresh_install_creates_prime_and_records_it(fake_storage_provider):
    await _seed_template(fake_storage_provider)
    registry = _Registry()
    created = await ensure_default_workspace(fake_storage_provider, workspace_registry=registry)
    assert created == "prime"
    row = await fake_storage_provider.get_storage(Workspace).get("prime")
    assert row.name == "prime"
    assert row.template_id == RESERVED_LOCAL_WORKSPACE_TEMPLATE
    assert row.template_snapshot is not None
    assert row.template_snapshot.id == RESERVED_LOCAL_WORKSPACE_TEMPLATE
    assert row.provider_id == "local"
    assert row.phase == "running"
    assert await _recorded(fake_storage_provider) == "prime"
    assert registry.ids == ["prime"], "the live instance is pinned to the id the row is keyed by"


async def test_second_pass_does_not_materialise_again(fake_storage_provider):
    await _seed_template(fake_storage_provider)
    registry = _Registry()
    await ensure_default_workspace(fake_storage_provider, workspace_registry=registry)
    again = await ensure_default_workspace(fake_storage_provider, workspace_registry=registry)
    assert again is None
    assert registry.calls == 1
    assert await _ids(fake_storage_provider) == ["prime"]


async def test_missing_template_is_a_no_op_and_records_nothing(fake_storage_provider):
    registry = _Registry()
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=registry) is None
    assert registry.calls == 0
    assert await _recorded(fake_storage_provider) is None, "a deferred pass names no default it cannot create"


async def test_no_registry_defers_and_records_nothing(fake_storage_provider):
    await _seed_template(fake_storage_provider)
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=None) is None
    assert await _recorded(fake_storage_provider) is None
    assert await _ids(fake_storage_provider) == []


async def test_template_id_overrides_the_reserved_default(fake_storage_provider):
    """The deployment declares its template; the seed honours it."""
    await _seed_template(fake_storage_provider, "cluster-default", provider_id="k3s")
    created = await ensure_default_workspace(
        fake_storage_provider, workspace_registry=_Registry(), template_id="cluster-default",
    )
    assert created == "prime"
    row = await fake_storage_provider.get_storage(Workspace).get("prime")
    assert row.template_id == "cluster-default"
    assert row.provider_id == "k3s"


async def test_missing_declared_template_defers_rather_than_falls_back(fake_storage_provider):
    """A typo'd template must not silently seed onto local instead."""
    await _seed_template(fake_storage_provider)
    registry = _Registry()
    created = await ensure_default_workspace(
        fake_storage_provider, workspace_registry=registry, template_id="no-such-template",
    )
    assert created is None
    assert registry.calls == 0
    assert await _recorded(fake_storage_provider) is None


# ---- an install that predates the record --------------------------------------------------------------------------------------


async def test_a_baselined_install_adopts_the_legacy_row_instead_of_creating_prime(fake_storage_provider):
    """Migrations were baselined (auto_bootstrap off), so m010 never ran: the pass adopts, it does not duplicate."""
    await _seed_template(fake_storage_provider)
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer", name="primer"))
    registry = _Registry()
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=registry) is None
    assert registry.calls == 0
    assert await _recorded(fake_storage_provider) == "primer"
    assert (await ws_store.get("primer")).name == "prime"
    assert await ws_store.get("prime") is None
    assert await _ids(fake_storage_provider) == ["primer"]


async def test_a_user_workspace_called_prime_is_adopted(fake_storage_provider):
    await _seed_template(fake_storage_provider)
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("prime", name="my stuff"))
    registry = _Registry()
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=registry) is None
    assert registry.calls == 0
    assert await _recorded(fake_storage_provider) == "prime"
    assert (await ws_store.get("prime")).name == "my stuff", "adopting a prime row records it; it does not rename a person's workspace"


async def test_the_legacy_row_wins_over_a_user_workspace_called_prime(fake_storage_provider):
    """Both exist and nothing is recorded: one default, never two. The legacy id is the one every older install means."""
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer", name="primer"))
    await ws_store.create(_row("prime", name="my stuff"))
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=_Registry()) is None
    assert await _recorded(fake_storage_provider) == "primer"
    assert (await ws_store.get("prime")).name == "my stuff"


async def test_a_missing_recorded_row_is_recreated_under_the_recorded_id(fake_storage_provider):
    """The recorded id is the default whatever the legacy constant says; a row that went missing comes back under it."""
    await _seed_template(fake_storage_provider)
    await fake_storage_provider.set_default_workspace_id("primer")
    registry = _Registry()
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=registry) == "primer"
    assert (await fake_storage_provider.get_storage(Workspace).get("primer")).name == "prime"
    assert registry.ids == ["primer"]
    assert await _ids(fake_storage_provider) == ["primer"]


async def test_a_recorded_id_is_never_replaced_by_the_pass(fake_storage_provider):
    await fake_storage_provider.set_default_workspace_id("home")
    await fake_storage_provider.get_storage(Workspace).create(_row("home", name="home"))
    await fake_storage_provider.get_storage(Workspace).create(_row("primer", name="primer"))
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=_Registry()) is None
    assert await _recorded(fake_storage_provider) == "home"
    assert (await fake_storage_provider.get_storage(Workspace).get("primer")).name == "primer", "a recorded install is never re-adopted"


# ---- several processes at once ------------------------------------------------------------------------------------------------


class _YieldingRegistry(_Registry):
    """Gives the other passes a turn while this one materialises, so every pass is past the "no row yet" check before any writes it."""

    async def materialise(self, *, template, overrides=None, workspace_id=None):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return await super().materialise(template=template, overrides=overrides, workspace_id=workspace_id)


def _yield_on_every_read(store) -> None:
    """Make every ``get`` of ``store`` a suspension point, so passes run through each read-then-write step side by side."""
    real_get = store.get

    async def _get(id, *, conn=None):  # noqa: A002
        await asyncio.sleep(0)
        return await real_get(id, conn=conn)

    store.get = _get


async def test_two_concurrent_passes_make_one_default_workspace(fake_storage_provider):
    await _seed_template(fake_storage_provider)
    registry = _YieldingRegistry()
    results = await asyncio.gather(
        *(ensure_default_workspace(fake_storage_provider, workspace_registry=registry) for _ in range(2))
    )
    assert registry.calls == 2, "both passes saw no row, so the race is real"
    assert sorted(r for r in results if r) == ["prime"], "exactly one pass reports having created it"
    assert await _ids(fake_storage_provider) == ["prime"]
    assert await _recorded(fake_storage_provider) == "prime"


async def test_two_concurrent_passes_adopt_the_legacy_row_once(fake_storage_provider):
    await _seed_template(fake_storage_provider)
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer", name="primer"))
    _yield_on_every_read(ws_store)
    registry = _Registry()
    results = await asyncio.gather(
        *(ensure_default_workspace(fake_storage_provider, workspace_registry=registry) for _ in range(3))
    )
    assert results == [None, None, None]
    assert registry.calls == 0
    assert await _recorded(fake_storage_provider) == "primer"
    assert await _ids(fake_storage_provider) == ["primer"]
    assert (await ws_store.get("primer")).name == "prime"


# ---- the delete guard can name the default at every instant (#770 review, binding) ---------------------------------------------


class _GuardProbe:
    """Tries to delete the default workspace through the REAL ``WorkspaceRegistry.destroy`` and notes the outcome."""

    def __init__(self, sp, workspace_id: str) -> None:
        self._registry = WorkspaceRegistry(sp)
        self._workspace_id = workspace_id
        self.outcomes: list[str] = []

    async def try_destroy(self, when: str) -> None:
        try:
            await self._registry.destroy(self._workspace_id)
        except ReservedWorkspaceError:
            self.outcomes.append(f"{when}: refused")
        except Exception as exc:  # noqa: BLE001 - the point is that it is NOT refused by the guard
            self.outcomes.append(f"{when}: slipped past the guard ({type(exc).__name__})")
        else:
            self.outcomes.append(f"{when}: slipped past the guard (deleted)")


async def test_the_default_is_recorded_before_the_row_exists(fake_storage_provider):
    """``destroy('prime')`` is refused while the backend is being materialised AND in the instant after the row is written.

    The row is written last, so a record written after it would leave a gap in which the row exists and the resolver
    answers ``None``. The id is recorded first: the guard names it at every instant of the pass.
    """
    await _seed_template(fake_storage_provider)
    probe = _GuardProbe(fake_storage_provider, "prime")
    ws_store = fake_storage_provider.get_storage(Workspace)

    class _ProbingRegistry(_Registry):
        async def materialise(self, *, template, overrides=None, workspace_id=None):
            assert await ws_store.get("prime") is None, "materialise runs before the row exists"
            await probe.try_destroy("during materialise")
            return await super().materialise(template=template, overrides=overrides, workspace_id=workspace_id)

    real_create = ws_store.create

    async def _create_then_probe(entity, *, conn=None):
        stored = await real_create(entity, conn=conn)
        await probe.try_destroy("right after the row is written")
        return stored

    ws_store.create = _create_then_probe  # type: ignore[method-assign]
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=_ProbingRegistry()) == "prime"
    assert probe.outcomes == ["during materialise: refused", "right after the row is written: refused"]
    assert await ws_store.get("prime") is not None, "the refused deletes left the row alone"


async def test_a_failed_materialise_keeps_the_record_and_the_next_pass_creates_the_row(fake_storage_provider):
    await _seed_template(fake_storage_provider)

    class _Boom:
        async def materialise(self, *, template, overrides=None, workspace_id=None):
            raise RuntimeError("backend down")

    with pytest.raises(RuntimeError, match="backend down"):
        await ensure_default_workspace(fake_storage_provider, workspace_registry=_Boom())
    assert await _recorded(fake_storage_provider) == "prime", "recorded before the attempt, so the guard already names it"
    assert await _ids(fake_storage_provider) == []

    registry = _Registry()
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=registry) == "prime"
    assert registry.ids == ["prime"]
    assert await _ids(fake_storage_provider) == ["prime"]


async def test_a_create_conflict_is_a_concurrent_creator_not_an_error(fake_storage_provider):
    await _seed_template(fake_storage_provider)
    ws_store = fake_storage_provider.get_storage(Workspace)

    async def _lose_the_race(entity, *, conn=None):
        raise ConflictError("another process created it")

    ws_store.create = _lose_the_race  # type: ignore[method-assign]
    assert await ensure_default_workspace(fake_storage_provider, workspace_registry=_Registry()) is None
    assert await _recorded(fake_storage_provider) == "prime"
