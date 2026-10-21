"""Migration 11: the five seeded agents move to the core kit, in place.

Rows are written exactly as v1 shipped them (the freeze in
``primer.bootstrap.seeded_agents_v1``), through the migration-local
``Agent`` view, so the test exercises the same read path the migration
takes. These tests pin WHAT the migration writes and leaves; the runner
itself is pinned by ``tests/storage/test_migrations.py``.
"""
from __future__ import annotations

import logging

from primer.bootstrap.operator_defaults import (
    OPERATOR_PROMPT,
    OPERATOR_TOOLS,
    SEEDED_AGENT_FACTORIES,
)
from primer.bootstrap.seeded_agents_v1 import SEEDED_AGENTS_V1
from primer.model.storage import OffsetPage
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m011_seeded_agents_core_kit import (
    Agent as AgentView,
)
from primer.storage.migrations.m011_seeded_agents_core_kit import (
    M011SeededAgentsCoreKit,
)


def _write_v1_rows(sp, *, profile_id: str = "custom") -> None:
    """Write the five seeded agents as v1 shipped them."""
    agents = sp.get_storage(AgentView)
    for agent_id, v1 in SEEDED_AGENTS_V1.items():
        agents._data[agent_id] = AgentView(
            id=agent_id,
            description=v1.description,
            model={"profile_id": profile_id},
            tools=list(v1.tools),
            system_prompt=list(v1.system_prompt),
        )


async def test_an_unedited_v1_agent_is_upgraded_and_keeps_its_profile(
    fake_storage_provider,
):
    _write_v1_rows(fake_storage_provider)
    await M011SeededAgentsCoreKit().apply(fake_storage_provider)

    agents = fake_storage_provider.get_storage(AgentView)
    row = await agents.get("operator")
    assert row.tools == list(OPERATOR_TOOLS)
    assert row.system_prompt == list(OPERATOR_PROMPT)
    assert row.model.profile_id == "custom"


async def test_other_fields_survive(fake_storage_provider):
    v1 = SEEDED_AGENTS_V1["operator"]
    agents = fake_storage_provider.get_storage(AgentView)
    agents._data["operator"] = AgentView(
        id="operator",
        description=v1.description,
        model={"profile_id": "custom"},
        tools=list(v1.tools),
        system_prompt=list(v1.system_prompt),
        temperature=0.2,
        max_tool_turns=7,
    )
    await M011SeededAgentsCoreKit().apply(fake_storage_provider)

    row = await agents.get("operator")
    assert row.tools == list(OPERATOR_TOOLS)
    assert row.temperature == 0.2
    assert row.max_tool_turns == 7


async def test_tool_order_does_not_count_as_an_edit(fake_storage_provider):
    v1 = SEEDED_AGENTS_V1["operator"]
    agents = fake_storage_provider.get_storage(AgentView)
    agents._data["operator"] = AgentView(
        id="operator",
        description=v1.description,
        model={"profile_id": "custom"},
        tools=list(reversed(v1.tools)),
        system_prompt=list(v1.system_prompt),
    )
    await M011SeededAgentsCoreKit().apply(fake_storage_provider)

    row = await agents.get("operator")
    assert row.tools == list(OPERATOR_TOOLS)
    assert row.system_prompt == list(OPERATOR_PROMPT)


async def test_an_edited_agent_is_left_alone_with_one_warning(
    fake_storage_provider, caplog,
):
    v1 = SEEDED_AGENTS_V1["builder"]
    agents = fake_storage_provider.get_storage(AgentView)
    agents._data["builder"] = AgentView(
        id="builder",
        description=v1.description,
        model={"profile_id": "custom"},
        tools=list(v1.tools),
        system_prompt=["mine"],
    )
    with caplog.at_level(logging.WARNING):
        await M011SeededAgentsCoreKit().apply(fake_storage_provider)

    row = await agents.get("builder")
    assert row.system_prompt == ["mine"]
    assert len(
        [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "builder" in r.getMessage()
        ]
    ) == 1


async def test_a_second_run_changes_nothing_and_warns_nothing(
    fake_storage_provider, caplog,
):
    _write_v1_rows(fake_storage_provider)
    migration = M011SeededAgentsCoreKit()
    await migration.apply(fake_storage_provider)
    agents = fake_storage_provider.get_storage(AgentView)
    before = {
        agent_id: (await agents.get(agent_id)).model_dump()
        for agent_id in SEEDED_AGENT_FACTORIES
    }
    with caplog.at_level(logging.WARNING):
        await migration.apply(fake_storage_provider)
    after = {
        agent_id: (await agents.get(agent_id)).model_dump()
        for agent_id in SEEDED_AGENT_FACTORIES
    }
    assert after == before
    assert not [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "was edited" in r.getMessage()
    ]


async def test_a_missing_agent_is_skipped(fake_storage_provider):
    await M011SeededAgentsCoreKit().apply(fake_storage_provider)

    agents = fake_storage_provider.get_storage(AgentView)
    page = await agents.list(OffsetPage(offset=0, length=100))
    assert page.items == []


def test_is_registered_as_version_11():
    assert isinstance(MIGRATIONS[10], M011SeededAgentsCoreKit)
    assert MIGRATIONS[10].version == 11
