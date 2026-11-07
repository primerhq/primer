"""Migration 30: every unedited seeded agent moves to the current (v4) definitions (wiki-first M1).

v4 rewrites the seeded prompts around the wiki loop (the purpose-mapped
index, then wiki_search, then wiki_read, then the exact scoped tools the
page names, stopping at the approval gate and never exploring the raw REST
API or probing the database for a documented flow) and adds the operator's
guarded identity block. Rows are written as each earlier build seeded them
(the freezes in ``primer.bootstrap.seeded_agents_v1``, now including v3) and
the upgrade runs through ``run_migrations`` from the schema version a
deployed install was at, so the tests cover the chain an upgrade really
takes: a deployed install at the latest version before this build carries
v3 rows.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from primer.bootstrap.operator_defaults import (
    OPERATOR_IDENTITY_PROMPT_PART,
    SEEDED_AGENT_FACTORIES,
    WORKSPACE_INSTRUCTIONS_PROMPT_PART,
)
from primer.bootstrap.seeded_agents_v1 import (
    SEEDED_AGENTS_AUGUST,
    SEEDED_AGENTS_V1,
    SEEDED_AGENTS_V2,
    SEEDED_AGENTS_V3,
    SeededDefinition,
)
from primer.model.storage import OffsetPage
from primer.storage.migrations import LATEST_VERSION, MIGRATIONS, run_migrations
from primer.storage.migrations.m011_seeded_agents_core_kit import Agent as AgentView
from primer.storage.migrations.m030_seeded_agents_v4 import M030SeededAgentsV4

_M030_PRIOR_VERSION = M030SeededAgentsV4.version - 1

#: Every build that seeded agents before v4, oldest first (v3 is the base).
_BUILDS: dict[str, dict[str, SeededDefinition]] = {
    **SEEDED_AGENTS_AUGUST,
    "v1": SEEDED_AGENTS_V1,
    "v2": SEEDED_AGENTS_V2,
    "v3": SEEDED_AGENTS_V3,
}


async def _write(sp, agent_id: str, definition: SeededDefinition, **extra) -> None:
    await sp.get_storage(AgentView).create(
        AgentView(
            id=agent_id,
            description=definition.description,
            model={"profile_id": "custom"},
            tools=list(definition.tools),
            system_prompt=list(definition.system_prompt),
            **extra,
        )
    )


async def _at_schema(sp, version: int) -> None:
    await sp.set_schema_version(version, migrated_at=datetime.now(UTC))


def _current(agent_id: str):
    return SEEDED_AGENT_FACTORIES[agent_id]("custom")


async def _assert_current(sp, agent_id: str) -> None:
    row = await sp.get_storage(AgentView).get(agent_id)
    current = _current(agent_id)
    assert row.description == current.description, agent_id
    assert row.system_prompt == current.system_prompt, agent_id
    assert row.tools == current.tools, agent_id
    assert row.model.profile_id == "custom", agent_id


def _warnings_naming(caplog, agent_id: str) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "was edited" in r.getMessage()
        and repr(agent_id) in r.getMessage()
    ]


@pytest.mark.parametrize("build", list(_BUILDS))
async def test_an_install_seeded_at_a_shipped_build_is_upgraded(sqlite_provider, build):
    """An unedited row of ANY earlier build (v3 included) reaches v4 on an upgrade."""
    for agent_id, definition in _BUILDS[build].items():
        await _write(sqlite_provider, agent_id, definition)
    # a deployed install at the version this migration lands on: the chain from here is this migration alone
    await _at_schema(sqlite_provider, _M030_PRIOR_VERSION)

    assert await run_migrations(sqlite_provider, is_fresh_install=False) == LATEST_VERSION

    for agent_id in _BUILDS[build]:
        await _assert_current(sqlite_provider, agent_id)
    operator = await sqlite_provider.get_storage(AgentView).get("operator")
    assert WORKSPACE_INSTRUCTIONS_PROMPT_PART in operator.system_prompt
    assert OPERATOR_IDENTITY_PROMPT_PART in operator.system_prompt
    assert "workspaces__message_session" in operator.tools


async def test_the_deployed_install_reaches_v4(sqlite_provider):
    """The headline case: a v3 row (what every deployed install carries) reaches v4."""
    for agent_id, definition in SEEDED_AGENTS_V3.items():
        await _write(sqlite_provider, agent_id, definition)
    await _at_schema(sqlite_provider, _M030_PRIOR_VERSION)

    await run_migrations(sqlite_provider, is_fresh_install=False)

    for agent_id in SEEDED_AGENTS_V3:
        await _assert_current(sqlite_provider, agent_id)
    for agent_id in SEEDED_AGENT_FACTORIES:
        row = await sqlite_provider.get_storage(AgentView).get(agent_id)
        joined = "\n".join(row.system_prompt)
        assert "purpose-map" in joined, agent_id  # the loop's first stop
        assert "raw REST API" in joined, agent_id  # the no-exploration rule


async def test_v4_carries_the_identity_block_and_the_loop(sqlite_provider):
    for agent_id, definition in SEEDED_AGENTS_V3.items():
        await _write(sqlite_provider, agent_id, definition)
    await _at_schema(sqlite_provider, _M030_PRIOR_VERSION)

    await run_migrations(sqlite_provider, is_fresh_install=False)

    operator = await sqlite_provider.get_storage(AgentView).get("operator")
    joined = "\n".join(operator.system_prompt)
    # the identity block is a GUARDED jinja section: the guard is in the shipped text
    assert "{% if ctx.identity %}" in joined
    assert "{% else %}" in joined and "{% endif %}" in joined
    assert "person id" in joined
    assert "runbooks/new-functionality" in joined  # the ladder's construction rung


async def test_an_edited_agent_is_left_alone_with_one_warning(sqlite_provider, caplog):
    v3 = SEEDED_AGENTS_V3
    await _write(sqlite_provider, "builder", v3["builder"]._replace(system_prompt=("mine",)))
    await _write(sqlite_provider, "operator", v3["operator"]._replace(tools=v3["operator"].tools + ("web__web_fetch",)))

    with caplog.at_level(logging.WARNING):
        await M030SeededAgentsV4().apply(sqlite_provider)

    agents = sqlite_provider.get_storage(AgentView)
    builder = await agents.get("builder")
    assert builder.system_prompt == ["mine"]
    assert builder.description == v3["builder"].description
    operator = await agents.get("operator")
    assert "web__web_fetch" in operator.tools
    assert operator.system_prompt == list(v3["operator"].system_prompt)
    assert len(_warnings_naming(caplog, "builder")) == 1
    assert len(_warnings_naming(caplog, "operator")) == 1


async def test_tool_order_does_not_count_as_an_edit(sqlite_provider):
    v3 = SEEDED_AGENTS_V3["operator"]
    await _write(sqlite_provider, "operator", v3._replace(tools=tuple(reversed(v3.tools))))

    await M030SeededAgentsV4().apply(sqlite_provider)

    await _assert_current(sqlite_provider, "operator")


async def test_other_fields_survive(sqlite_provider):
    await _write(
        sqlite_provider, "operator", SEEDED_AGENTS_V3["operator"],
        temperature=0.2, max_tool_turns=7,
    )

    await M030SeededAgentsV4().apply(sqlite_provider)

    row = await sqlite_provider.get_storage(AgentView).get("operator")
    assert row.system_prompt == _current("operator").system_prompt
    assert row.temperature == 0.2
    assert row.max_tool_turns == 7
    assert row.model.profile_id == "custom"


async def test_a_second_run_changes_nothing_and_warns_nothing(sqlite_provider, caplog):
    for agent_id, definition in SEEDED_AGENTS_V3.items():
        await _write(sqlite_provider, agent_id, definition)
    migration = M030SeededAgentsV4()
    await migration.apply(sqlite_provider)
    agents = sqlite_provider.get_storage(AgentView)
    before = {a: (await agents.get(a)).model_dump() for a in SEEDED_AGENT_FACTORIES}

    with caplog.at_level(logging.WARNING):
        await migration.apply(sqlite_provider)

    after = {a: (await agents.get(a)).model_dump() for a in SEEDED_AGENT_FACTORIES}
    assert after == before
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_missing_agent_is_skipped(sqlite_provider):
    await M030SeededAgentsV4().apply(sqlite_provider)

    page = await sqlite_provider.get_storage(AgentView).list(OffsetPage(offset=0, length=100))
    assert page.items == []


async def test_a_fresh_install_baselines_and_seeds_v4(sqlite_provider):
    from primer.bootstrap.seed import ensure_seeded_agents
    from primer.model.model_profile import ModelProfile

    assert await run_migrations(sqlite_provider, is_fresh_install=True) == LATEST_VERSION
    assert LATEST_VERSION >= 30
    await sqlite_provider.get_storage(ModelProfile).create(
        ModelProfile(
            id="custom", description="default", provider_id="llm-1",
            model_name="qwen", context_length=32000,
        )
    )

    assert set(await ensure_seeded_agents(sqlite_provider)) == set(SEEDED_AGENT_FACTORIES)

    for agent_id in SEEDED_AGENT_FACTORIES:
        await _assert_current(sqlite_provider, agent_id)
    operator = await sqlite_provider.get_storage(AgentView).get("operator")
    assert WORKSPACE_INSTRUCTIONS_PROMPT_PART in operator.system_prompt
    assert OPERATOR_IDENTITY_PROMPT_PART in operator.system_prompt


def test_is_registered_as_version_30():
    assert isinstance(MIGRATIONS[29], M030SeededAgentsV4)
    assert MIGRATIONS[29].version == 30
