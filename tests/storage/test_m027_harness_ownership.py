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
    assert MIGRATIONS[-1].version == 27 and isinstance(MIGRATIONS[-1], M027HarnessOwnership)


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
