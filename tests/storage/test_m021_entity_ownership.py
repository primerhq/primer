"""Migration 21: existing agents, graphs, toolsets, workspace templates and channels get an owner (access-control spec 8)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from primer.bootstrap.defaults import RESERVED_LOCAL_WORKSPACE_TEMPLATE, RESERVED_WORKSPACE_TEMPLATES
from primer.bootstrap.operator_defaults import SEEDED_AGENT_FACTORIES
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.model.workspace import WorkspaceTemplate
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m021_entity_ownership import M021EntityOwnership
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


async def _users(sp, *rows: tuple[str, str, int]) -> None:
    store = sp.get_storage(User)
    for uid, role, age_days in rows:
        await store.create(
            User(id=uid, username=uid, password_hash=None, role=role, created_at=_T0 + timedelta(days=age_days))
        )


def _agent(agent_id: str, access: Access | None = None) -> Agent:
    agent = Agent(id=agent_id, description="d", model=AgentModel(profile_id="p"))
    return agent if access is None else agent.model_copy(update={"access": access})


async def test_registered_as_version_21():
    registered = next(m for m in MIGRATIONS if m.version == 21)
    assert isinstance(registered, M021EntityOwnership)


async def test_rows_go_to_the_oldest_admin_and_seeded_ones_become_public(sp):
    await _users(sp, ("u-user", "user", 0), ("u-old-admin", "admin", 1), ("u-new-admin", "admin", 5))
    agents = sp.get_storage(Agent)
    await agents.create(_agent("custom"))
    await agents.create(_agent("operator"))
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(**RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE]).model_copy(update={"access": Access()}))

    await M021EntityOwnership().apply(sp)

    custom = await agents.get("custom")
    assert custom.access.owners == ["u-old-admin"] and custom.access.public is False
    operator = await agents.get("operator")
    assert operator.access.owners == [] and operator.access.public is True
    template = await sp.get_storage(WorkspaceTemplate).get(RESERVED_LOCAL_WORKSPACE_TEMPLATE)
    assert template.access.public is True and template.access.owners == []


async def test_a_row_that_already_has_access_is_left_alone(sp):
    await _users(sp, ("u-admin", "admin", 0))
    agents = sp.get_storage(Agent)
    await agents.create(_agent("mine", Access(owners=["u-other"])))
    await agents.create(_agent("open", Access(public=True)))
    await M021EntityOwnership().apply(sp)
    assert (await agents.get("mine")).access.owners == ["u-other"]
    assert (await agents.get("open")).access == Access(public=True)


async def test_without_an_admin_rows_are_left_for_admins(sp):
    await _users(sp, ("u-user", "user", 0))
    agents = sp.get_storage(Agent)
    await agents.create(_agent("custom"))
    await M021EntityOwnership().apply(sp)
    assert (await agents.get("custom")).access == Access()


async def test_rerunning_changes_nothing(sp):
    await _users(sp, ("u-admin", "admin", 0))
    agents = sp.get_storage(Agent)
    await agents.create(_agent("custom"))
    await agents.create(_agent("operator"))
    await M021EntityOwnership().apply(sp)
    first = {a.id: a.access for a in (await agents.list(_page())).items}
    await M021EntityOwnership().apply(sp)
    second = {a.id: a.access for a in (await agents.list(_page())).items}
    assert first == second


def _page():
    from primer.model.storage import OffsetPage

    return OffsetPage(offset=0, length=50)


def test_seeded_agents_and_the_default_template_are_public_with_no_owner():
    for factory in SEEDED_AGENT_FACTORIES.values():
        access = factory("profile-1").access
        assert access.public is True and access.owners == []
    for spec in RESERVED_WORKSPACE_TEMPLATES.values():
        access = WorkspaceTemplate(**spec).access
        assert access.public is True and access.owners == []
