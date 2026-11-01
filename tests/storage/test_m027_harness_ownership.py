"""Migration 27: harnesses get an owner; what they installed copies their access (access-control Phase 3, Task 6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.collection import Collection
from primer.model.graph import Graph
from primer.model.harness import Harness
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m027_harness_ownership import M027HarnessOwnership
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


def _harness(hid: str, access: Access | None = None) -> Harness:
    h = Harness(id=hid, slug=hid.replace("_", "-"), name=hid, git_url="https://github.com/x/y", created_at=_T0)
    return h if access is None else h.model_copy(update={"access": access})


def test_registered_as_version_27():
    assert MIGRATIONS[26].version == 27 and isinstance(MIGRATIONS[26], M027HarnessOwnership)


async def test_harnesses_get_the_first_admin_and_installed_rows_copy_it(sp):
    users = sp.get_storage(User)
    await users.create(User(id="late", username="late", created_at=_T0 + timedelta(days=3), role="admin"))
    await users.create(User(id="root", username="root", created_at=_T0, role="admin"))
    await users.create(User(id="off", username="off", created_at=_T0 - timedelta(days=9), role="admin", disabled=True))
    shared = Access(owners=["bob"], grants={"amy": "read"}, rev=2)
    await sp.get_storage(Harness).create(_harness("hns_unset"))
    await sp.get_storage(Harness).create(_harness("hns_shared", shared))
    agent = Agent(id="ag-1", description="d", harness_id="hns_unset", model={"profile_id": "p--m"}, tools=[], system_prompt=[])
    await sp.get_storage(Agent).create(agent)
    await sp.get_storage(Graph).create(Graph(id="gr-1", description="g", harness_id="hns_shared", nodes=[], edges=[]))
    await sp.get_storage(Collection).create(Collection(id="kb-1", description="k", harness_id="hns_gone", access=Access(owners=["zed"])))
    await sp.get_storage(Agent).create(agent.model_copy(update={"id": "ag-own", "harness_id": None}))

    await M027HarnessOwnership().apply(sp)
    await M027HarnessOwnership().apply(sp)  # converges

    assert (await sp.get_storage(Harness).get("hns_unset")).access.owners == ["root"]
    assert (await sp.get_storage(Harness).get("hns_shared")).access == shared
    assert (await sp.get_storage(Agent).get("ag-1")).access.owners == ["root"]
    assert (await sp.get_storage(Graph).get("gr-1")).access == shared
    assert (await sp.get_storage(Collection).get("kb-1")).access.owners == ["zed"]  # its harness is gone: untouched
    assert (await sp.get_storage(Agent).get("ag-own")).access.owners == []  # not installed by a harness


# -- golden characterization (PERF-T16) ---------------------------------------------------------------------------------------------------

from primer.storage.migrations._rows import read_all  # noqa: E402
from primer.storage.migrations.m027_harness_ownership import Agent as AgentView  # noqa: E402
from primer.storage.migrations.m027_harness_ownership import Collection as CollectionView  # noqa: E402
from primer.storage.migrations.m027_harness_ownership import Graph as GraphView  # noqa: E402
from primer.storage.migrations.m027_harness_ownership import Harness as HarnessView  # noqa: E402
from primer.storage.migrations.m027_harness_ownership import Toolset as ToolsetView  # noqa: E402
from primer.storage.migrations.m027_harness_ownership import User as UserView  # noqa: E402
from primer.storage.migrations.m027_harness_ownership import _owned_by, _unset  # noqa: E402
from tests.storage._migration_golden import assert_golden, bulk, insert_raw  # noqa: E402


async def _old_apply(sp) -> None:
    """Migration 27 as it was before PERF-T16, frozen: read each table whole, then ``update`` per row."""
    admins = [u for u in await read_all(sp.get_storage(UserView)) if u.role == "admin" and not u.disabled and u.created_at is not None]
    first_admin = min(admins, key=lambda u: u.created_at).id if admins else None
    harnesses = sp.get_storage(HarnessView)
    by_id = {}
    for row in await read_all(harnesses):
        access = row.access
        if _unset(access) and first_admin is not None:
            access = _owned_by(first_admin)
            await harnesses.update(row.model_copy(update={"access": access}))
        by_id[row.id] = access
    for model in (AgentView, GraphView, ToolsetView, CollectionView):
        store = sp.get_storage(model)
        for row in await read_all(store):
            access = by_id.get(row.harness_id) if row.harness_id else None
            if not access or row.access == access:
                continue
            await store.update(row.model_copy(update={"access": access}))


def _golden_seed(with_admin: bool):
    async def seed(sp) -> None:
        users = {"u1": {"role": "user", "created_at": "2025-01-01T00:00:00Z"}, **bulk("ub", 450, lambda i: {"role": "user"})}
        if with_admin:
            users |= {"admin-a": {"role": "admin", "created_at": "2025-03-01T00:00:00Z"}, "zz-admin": {"role": "admin", "created_at": "2025-02-01T00:00:00Z"}}
        await insert_raw(sp, UserView, users)
        shared = {"owners": ["u1"], "grants": {"u2": "use"}, "public": False, "members": ["u:u1"], "rev": 3}
        await insert_raw(sp, HarnessView, {
            "h-unset": {"slug": "a"},
            "h-empty": {"access": {}},
            "h-null": {"access": None},
            "h-shared": {"access": shared},
            "h-public": {"access": {"public": True}},
            **bulk("hb", 450, lambda i: {"n": i}),
        })
        await insert_raw(sp, AgentView, {
            "a-unset": {"harness_id": "h-unset", "description": "d"},
            "a-shared": {"harness_id": "h-shared", "access": {"owners": ["other"]}},
            "a-equal": {"harness_id": "h-shared", "access": shared},
            "a-gone": {"harness_id": "h-gone", "access": {"owners": ["keep"]}},
            "a-none": {"access": None},
            "a-empty-harness": {"harness_id": ""},
            **bulk("ab", 450, lambda i: {"harness_id": f"hb{i:03d}" if i % 2 else "h-shared"}),
        })
        await insert_raw(sp, GraphView, {"g1": {"harness_id": "h-public"}, "g2": {}})
        await insert_raw(sp, ToolsetView, {"t1": {"harness_id": "h-empty", "kind": "mcp"}})
        await insert_raw(sp, CollectionView, {"c1": {"harness_id": "h-null"}, "c2": {"harness_id": "h-shared", "access": shared}})
    return seed


@pytest.mark.parametrize("with_admin", [True, False])
async def test_golden_rows_match_the_old_implementation_and_a_second_run_changes_nothing(tmp_path: Path, with_admin: bool) -> None:
    views = (HarnessView, AgentView, GraphView, ToolsetView, CollectionView, UserView)
    await assert_golden(tmp_path, _golden_seed(with_admin), views, _old_apply, M027HarnessOwnership().apply)
