"""Migration 15: every unedited seeded agent moves to the current (v3) definition (audit G7-04).

Rows are written as each earlier build seeded them (the freezes in
``primer.bootstrap.seeded_agents_v1``) into a real SQLite store, and the
upgrade runs through ``run_migrations`` from the schema version a deployed
install was at, so the tests cover the chain an upgrade really takes. The
headline case is an install seeded on 2026-08-19 to 08-22: migration 11
judged its operator and builder edited and left them with their
pre-core-kit grants.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from primer.bootstrap.operator_defaults import (
    SEEDED_AGENT_FACTORIES,
    WORKSPACE_INSTRUCTIONS_PROMPT_PART,
)
from primer.bootstrap.seeded_agents_v1 import (
    SEEDED_AGENTS_AUGUST,
    SEEDED_AGENTS_V1,
    SEEDED_AGENTS_V2,
    SeededDefinition,
)
from primer.model.storage import OffsetPage
from primer.storage.migrations import LATEST_VERSION, MIGRATIONS, run_migrations
from primer.storage.migrations.m011_seeded_agents_core_kit import Agent as AgentView

#: Every build that seeded agents before v3, oldest first.
_BUILDS: dict[str, dict[str, SeededDefinition]] = {
    **SEEDED_AGENTS_AUGUST,
    "v1": SEEDED_AGENTS_V1,
    "v2": SEEDED_AGENTS_V2,
}

_RETIRED_SEARCH = {"collections__grep_collection", "collections__semantic_search"}


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
    """An unedited row of ANY earlier build reaches v3 on an install that already ran migration 11."""
    for agent_id, definition in _BUILDS[build].items():
        await _write(sqlite_provider, agent_id, definition)
    await _at_schema(sqlite_provider, 11)

    assert await run_migrations(sqlite_provider, is_fresh_install=False) == LATEST_VERSION

    for agent_id in _BUILDS[build]:
        await _assert_current(sqlite_provider, agent_id)
    if "operator" in _BUILDS[build]:
        row = await sqlite_provider.get_storage(AgentView).get("operator")
        assert WORKSPACE_INSTRUCTIONS_PROMPT_PART in row.system_prompt
        assert "workspaces__message_session" in row.tools


async def test_the_audited_install_reaches_v3_through_the_whole_chain(sqlite_provider):
    """The G7-04 scenario: seeded at 5c78cca62 with grep_collection, upgraded from schema 5.

    Migration 6 rewrites the retired grant, and the rows then equal the
    5c78cca62 freeze; whichever of 11 and 15 sees them first moves them.
    """
    for agent_id, definition in SEEDED_AGENTS_AUGUST["5c78cca62"].items():
        tools = tuple(
            "collections__grep_collection" if t == "collections__search" else t
            for t in definition.tools
        )
        await _write(sqlite_provider, agent_id, definition._replace(tools=tools))
    await _at_schema(sqlite_provider, 5)

    await run_migrations(sqlite_provider, is_fresh_install=False)

    for agent_id in ("operator", "builder"):
        await _assert_current(sqlite_provider, agent_id)
        row = await sqlite_provider.get_storage(AgentView).get(agent_id)
        assert not set(row.tools) & _RETIRED_SEARCH, agent_id


async def test_an_edited_agent_is_left_alone_with_one_warning(sqlite_provider, caplog):
    from primer.storage.migrations.m015_seeded_agents_v3 import M015SeededAgentsV3

    august = SEEDED_AGENTS_AUGUST["8d8b10dd9"]["builder"]
    await _write(sqlite_provider, "builder", august._replace(system_prompt=("mine",)))
    v2 = SEEDED_AGENTS_V2["operator"]
    await _write(sqlite_provider, "operator", v2._replace(tools=v2.tools + ("web__web_fetch",)))

    with caplog.at_level(logging.WARNING):
        await M015SeededAgentsV3().apply(sqlite_provider)

    agents = sqlite_provider.get_storage(AgentView)
    builder = await agents.get("builder")
    assert builder.system_prompt == ["mine"]
    assert builder.description == august.description
    operator = await agents.get("operator")
    assert "web__web_fetch" in operator.tools
    assert operator.system_prompt == list(v2.system_prompt)
    assert len(_warnings_naming(caplog, "builder")) == 1
    assert len(_warnings_naming(caplog, "operator")) == 1


async def test_tool_order_does_not_count_as_an_edit(sqlite_provider):
    from primer.storage.migrations.m015_seeded_agents_v3 import M015SeededAgentsV3

    august = SEEDED_AGENTS_AUGUST["64704ff5d"]["operator"]
    await _write(sqlite_provider, "operator", august._replace(tools=tuple(reversed(august.tools))))

    await M015SeededAgentsV3().apply(sqlite_provider)

    await _assert_current(sqlite_provider, "operator")


async def test_other_fields_survive(sqlite_provider):
    from primer.storage.migrations.m015_seeded_agents_v3 import M015SeededAgentsV3

    await _write(
        sqlite_provider, "operator", SEEDED_AGENTS_V2["operator"],
        temperature=0.2, max_tool_turns=7,
    )

    await M015SeededAgentsV3().apply(sqlite_provider)

    row = await sqlite_provider.get_storage(AgentView).get("operator")
    assert row.system_prompt == _current("operator").system_prompt
    assert row.temperature == 0.2
    assert row.max_tool_turns == 7
    assert row.model.profile_id == "custom"


async def test_a_second_run_changes_nothing_and_warns_nothing(sqlite_provider, caplog):
    from primer.storage.migrations.m015_seeded_agents_v3 import M015SeededAgentsV3

    for agent_id, definition in SEEDED_AGENTS_AUGUST["8d8b10dd9"].items():
        await _write(sqlite_provider, agent_id, definition)
    migration = M015SeededAgentsV3()
    await migration.apply(sqlite_provider)
    agents = sqlite_provider.get_storage(AgentView)
    before = {a: (await agents.get(a)).model_dump() for a in SEEDED_AGENT_FACTORIES}

    with caplog.at_level(logging.WARNING):
        await migration.apply(sqlite_provider)

    after = {a: (await agents.get(a)).model_dump() for a in SEEDED_AGENT_FACTORIES}
    assert after == before
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_missing_agent_is_skipped(sqlite_provider):
    from primer.storage.migrations.m015_seeded_agents_v3 import M015SeededAgentsV3

    await M015SeededAgentsV3().apply(sqlite_provider)

    page = await sqlite_provider.get_storage(AgentView).list(OffsetPage(offset=0, length=100))
    assert page.items == []


class _StaleReads:
    """A provider whose agent reads return a snapshot taken before a concurrent edit."""

    def __init__(self, inner, stale: dict[str, dict]):
        self._inner = inner
        self._stale = stale

    def get_storage(self, cls):
        handle = self._inner.get_storage(cls)
        stale = self._stale

        class _Handle:
            async def get(self, agent_id):
                if agent_id in stale:
                    return cls(**stale[agent_id])
                return await handle.get(agent_id)

            def __getattr__(self, name):
                return getattr(handle, name)

        return _Handle()


async def test_a_description_edited_after_the_read_wins(sqlite_provider, caplog):
    """The write is guarded on the stored description: an edit that lands first is kept."""
    from primer.storage.migrations.m015_seeded_agents_v3 import M015SeededAgentsV3

    august = SEEDED_AGENTS_AUGUST["5c78cca62"]["operator"]
    await _write(sqlite_provider, "operator", august._replace(description="edited meanwhile"))
    snapshot = {
        "id": "operator",
        "description": august.description,
        "model": {"profile_id": "custom"},
        "tools": list(august.tools),
        "system_prompt": list(august.system_prompt),
    }

    with caplog.at_level(logging.WARNING):
        await M015SeededAgentsV3().apply(_StaleReads(sqlite_provider, {"operator": snapshot}))

    row = await sqlite_provider.get_storage(AgentView).get("operator")
    assert row.description == "edited meanwhile"
    assert row.system_prompt == list(august.system_prompt)
    assert [r for r in caplog.records if "operator" in r.getMessage() and r.levelno == logging.WARNING]


async def test_a_fresh_install_baselines_past_15_and_seeds_v3(sqlite_provider):
    from primer.bootstrap.seed import ensure_seeded_agents
    from primer.model.model_profile import ModelProfile

    assert await run_migrations(sqlite_provider, is_fresh_install=True) == LATEST_VERSION
    assert LATEST_VERSION >= 15
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
    assert not any("Follow it" in p for p in operator.system_prompt)


def test_is_registered_as_version_15():
    from primer.storage.migrations.m015_seeded_agents_v3 import M015SeededAgentsV3

    assert isinstance(MIGRATIONS[14], M015SeededAgentsV3)
    assert MIGRATIONS[14].version == 15
