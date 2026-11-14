"""Migration 34: every unedited seeded agent moves to the current (v5) definitions (wiki-first W6).

v5 adds the operator's moderator and bookkeeper role text to its prompt (the
moderator flow: plan with the planner, create a separate workspace, launch
the agents or sessions the plan names in it; the bookkeeper flow: survey
every readable workspace and its sessions' running/parked state via
``list_workspaces`` + ``find_sessions``), each pointing at its wiki runbook.
The other four seeded agents are unchanged. Rows are written as each earlier
build seeded them (the freezes in ``primer.bootstrap.seeded_agents_v1``, now
including v4) and the upgrade runs through ``run_migrations`` from the
schema version a deployed install was at, so the tests cover the chain an
upgrade really takes: a deployed install at the latest version before this
build carries v4 rows.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from primer.bootstrap.operator_defaults import (
    SEEDED_AGENT_FACTORIES,
)
from primer.bootstrap.seeded_agents_v1 import (
    SEEDED_AGENTS_V4,
    SeededDefinition,
)
from primer.model.storage import OffsetPage
from primer.model.provider import PoolConfig, PostgresConfig, SqliteConfig
from primer.storage.migrations import LATEST_VERSION, MIGRATIONS, run_migrations
from primer.storage.migrations.m011_seeded_agents_core_kit import Agent as AgentView
from primer.storage.migrations.m034_seeded_agents_v5 import M034SeededAgentsV5
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_M034_PRIOR_VERSION = M034SeededAgentsV5.version - 1
_WHAT = "the W6 seeded-agent migration parity tests"


@pytest_asyncio.fixture(params=["sqlite", pytest.param("postgres", marks=postgres_marks(_WHAT))])
async def provider(request, tmp_path):
    """The SAME scenarios on SQLite and an isolated CI-owned Postgres schema."""
    if request.param == "sqlite":
        sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "w6.sqlite"))
    else:
        url = urlparse(require_postgres_url(_WHAT))
        sp = PostgresStorageProvider(PostgresConfig(
            hostname=url.hostname or "localhost", port=explicit_port(url),
            username=url.username or "primer", password=url.password or "",
            database=(url.path or "/").lstrip("/") or "postgres",
            db_schema=f"w6{uuid.uuid4().hex[:16]}", pool=PoolConfig(min_size=1, max_size=4),
        ))
    await sp.initialize()
    try:
        yield sp
    finally:
        if request.param == "postgres":
            async with sp.pool.acquire() as conn:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{sp.schema}" CASCADE')
        await sp.aclose()


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


async def test_an_install_seeded_at_v4_is_upgraded(provider):
    """The headline case: a v4 row (what every deployed install carries) reaches v5."""
    for agent_id, definition in SEEDED_AGENTS_V4.items():
        await _write(provider, agent_id, definition)
    # a deployed install at the version this migration lands on: the chain from here is this migration alone
    await _at_schema(provider, _M034_PRIOR_VERSION)

    assert await run_migrations(provider, is_fresh_install=False) == LATEST_VERSION

    for agent_id in SEEDED_AGENTS_V4:
        await _assert_current(provider, agent_id)


async def test_v5_carries_the_moderator_and_bookkeeper_role_text(provider):
    """Only the operator's prompt changes; the role text names both runbooks."""
    for agent_id, definition in SEEDED_AGENTS_V4.items():
        await _write(provider, agent_id, definition)
    await _at_schema(provider, _M034_PRIOR_VERSION)

    await run_migrations(provider, is_fresh_install=False)

    agents = provider.get_storage(AgentView)
    operator = await agents.get("operator")
    joined = "\n".join(operator.system_prompt)
    assert "runbooks/moderate-a-request" in joined  # the moderator flow's runbook
    assert "runbooks/workspace-status" in joined  # the bookkeeper flow's runbook
    assert "list_workspaces" in joined and "find_sessions" in joined
    # the other four agents are v4 unchanged
    for agent_id in ("builder", "planner", "explorer", "tool-runner"):
        row = await agents.get(agent_id)
        v4 = SEEDED_AGENTS_V4[agent_id]
        assert row.system_prompt == list(v4.system_prompt), agent_id
        assert row.description == v4.description, agent_id


async def test_an_edited_agent_is_left_alone_with_one_warning(provider, caplog):
    v4 = SEEDED_AGENTS_V4
    await _write(provider, "builder", v4["builder"]._replace(system_prompt=("mine",)))
    await _write(provider, "operator", v4["operator"]._replace(tools=v4["operator"].tools + ("web__web_fetch",)))

    with caplog.at_level(logging.WARNING):
        await M034SeededAgentsV5().apply(provider)

    agents = provider.get_storage(AgentView)
    builder = await agents.get("builder")
    assert builder.system_prompt == ["mine"]
    assert builder.description == v4["builder"].description
    operator = await agents.get("operator")
    assert "web__web_fetch" in operator.tools
    assert operator.system_prompt == list(v4["operator"].system_prompt)
    assert len(_warnings_naming(caplog, "builder")) == 1
    assert len(_warnings_naming(caplog, "operator")) == 1


async def test_tool_order_does_not_count_as_an_edit(provider):
    v4 = SEEDED_AGENTS_V4["operator"]
    await _write(provider, "operator", v4._replace(tools=tuple(reversed(v4.tools))))

    await M034SeededAgentsV5().apply(provider)

    await _assert_current(provider, "operator")


@pytest.mark.parametrize("field", ["description", "system_prompt", "tools"])
async def test_an_operator_edit_after_the_read_wins(provider, caplog, field):
    """Every field the upgrade replaces must still match its read generation."""
    v4 = SEEDED_AGENTS_V4["operator"]
    value = "my description" if field == "description" else ("my edited value",)
    edited = v4._replace(**{field: value})
    await _write(provider, "operator", edited)
    snapshot = {
        "id": "operator", "description": v4.description, "model": {"profile_id": "custom"},
        "system_prompt": list(v4.system_prompt), "tools": list(v4.tools),
    }

    class StaleReads:
        transaction = provider.transaction

        def get_storage(self, model):
            handle = provider.get_storage(model)

            class Handle:
                async def get(self, agent_id):
                    return model(**snapshot) if agent_id == "operator" else await handle.get(agent_id)

                async def patch_if(self, *args, **kwargs):
                    return await handle.patch_if(*args, **kwargs)

            return Handle()

    with caplog.at_level(logging.WARNING):
        await M034SeededAgentsV5().apply(StaleReads())
    row = await provider.get_storage(AgentView).get("operator")
    assert row.description == edited.description
    assert row.system_prompt == list(edited.system_prompt)
    assert row.tools == list(edited.tools)
    assert len(_warnings_naming(caplog, "operator")) == 1


@pytest.mark.parametrize("field", ["description", "system_prompt", "tools"])
async def test_an_editor_waits_for_the_recheck_transaction_then_its_edit_survives(provider, tmp_path, field):
    """The gap AFTER the locking write is protected by a real database lock.

    Postgres proves the editor is blocked by this connection's row lock
    through pg_blocking_pids, not just an arbitrary sleep. SQLite uses a
    SECOND provider/connection so only the database write lock can block
    it, not the migration provider's asyncio lock. Both then preserve the
    independently committed edit after the upgrade releases its lock.
    """
    await _write(provider, "operator", SEEDED_AGENTS_V4["operator"])
    locked, proceed, editing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    connections = []

    class PauseAfterLock:
        transaction = provider.transaction

        def get_storage(self, model):
            store = provider.get_storage(model)

            class Handle:
                get = store.get

                async def patch_if(self, *args, **kwargs):
                    out = await store.patch_if(*args, **kwargs)
                    connections.append(kwargs.get("conn"))
                    if len(connections) == 1:
                        locked.set()
                        await proceed.wait()
                    return out

            return Handle()

    @asynccontextmanager
    async def editor_connection():
        if isinstance(provider, PostgresStorageProvider):
            async with provider.pool.acquire() as conn:
                yield provider.get_storage(AgentView), conn
        else:
            peer = SqliteStorageProvider(SqliteConfig(path=tmp_path / "w6.sqlite"))
            await peer.initialize()
            try:
                other = peer.get_storage(AgentView)
                assert await other.get("operator") is not None  # ensure DDL before holding the lock
                yield other, None
            finally:
                await peer.aclose()

    value = "edited while upgrading" if field == "description" else ["edited while upgrading"]
    store = provider.get_storage(AgentView)

    async def edit(other, conn):
        editing.set()
        return await other.patch_if(
            "operator", {field: value},
            where={"description": [SEEDED_AGENTS_V4["operator"].description]}, conn=conn,
        )

    # Close/release the editor connection only after both tasks have ended,
    # including failure paths, so a timeout cannot leave a live query on it.
    async with editor_connection() as (other, conn):
        migration = asyncio.create_task(M034SeededAgentsV5().apply(PauseAfterLock()))
        editor = None
        try:
            async with asyncio.timeout(30):
                await locked.wait()
                editor = asyncio.create_task(edit(other, conn))
                await editing.wait()
                if isinstance(provider, PostgresStorageProvider):
                    async with provider.pool.acquire() as probe:
                        while not await probe.fetchval(
                            "SELECT $1 = ANY(pg_blocking_pids($2))",
                            connections[0].get_server_pid(), conn.get_server_pid(),
                        ):
                            assert not editor.done(), "editor did not wait on the migration's row lock"
                            await asyncio.sleep(0.01)
                else:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(editor), timeout=0.05)
                proceed.set()
                await migration
                assert await editor is not None
        finally:
            proceed.set()
            tasks = [migration, *([editor] if editor is not None else [])]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    assert len(connections) == 2 and connections[0] is connections[1]
    assert connections[0] is not None, "both writes must use the outer transaction connection"
    row = await store.get("operator")
    expected = _current("operator").model_dump(mode="json")
    for name in ("description", "system_prompt", "tools"):
        assert getattr(row, name) == (value if name == field else expected[name])


async def test_the_locking_write_and_upgrade_roll_back_together(provider):
    await _write(provider, "operator", SEEDED_AGENTS_V4["operator"])
    store = provider.get_storage(AgentView)
    before = (await store.get("operator")).model_dump()

    class FailAfterUpgrade:
        transaction = provider.transaction

        def get_storage(self, model):
            actual = provider.get_storage(model)

            class Handle:
                get = actual.get

                async def patch_if(self, *args, **kwargs):
                    out = await actual.patch_if(*args, **kwargs)
                    if "system_prompt" in args[1]:
                        raise RuntimeError("after the upgrade write, before commit")
                    return out

            return Handle()

    with pytest.raises(RuntimeError, match="before commit"):
        await M034SeededAgentsV5().apply(FailAfterUpgrade())
    assert (await store.get("operator")).model_dump() == before
    await M034SeededAgentsV5().apply(provider)
    await _assert_current(provider, "operator")  # transaction cleanup permits a real retry


@pytest.mark.parametrize("change,upgraded", [
    pytest.param({"tools": list(reversed(SEEDED_AGENTS_V4["operator"].tools))}, True, id="tool-order-is-not-an-edit"),
    pytest.param({"system_prompt": list(reversed(SEEDED_AGENTS_V4["operator"].system_prompt))}, False, id="prompt-order-is-an-edit"),
    pytest.param({"system_prompt": []}, False, id="empty-prompt-is-an-edit"),
    pytest.param({"tools": []}, False, id="empty-tools-is-an-edit"),
    pytest.param({"model": None}, False, id="null-profile-is-preserved"),
])
async def test_order_empty_lists_and_null_profile_keep_the_shipped_equality_rule(provider, change, upgraded):
    await _write(provider, "operator", SEEDED_AGENTS_V4["operator"])
    store = provider.get_storage(AgentView)
    row = await store.get("operator")
    row = type(row).model_validate({**row.model_dump(), **change})
    await store.update(row)

    await M034SeededAgentsV5().apply(provider)

    if upgraded:
        await _assert_current(provider, "operator")
    else:
        assert (await store.get("operator")).model_dump() == row.model_dump()


@pytest.mark.parametrize("reverse_keys", [False, True])
async def test_unrelated_json_order_null_and_scalar_types_are_not_reinterpreted(provider, reverse_keys):
    """No JSON text equality is involved; a field patch preserves these values."""
    items = [("null", None), ("boolean", True), ("number", 1), ("string", "1"), ("array", ["b", "a"])]
    extra = dict(reversed(items) if reverse_keys else items)
    await _write(provider, "operator", SEEDED_AGENTS_V4["operator"], preferences=extra)

    await M034SeededAgentsV5().apply(provider)

    row = await provider.get_storage(AgentView).get("operator")
    assert row.preferences == extra
    for key, value in extra.items():
        assert type(row.preferences[key]) is type(value), key  # bool must not become the number 1
    await _assert_current(provider, "operator")


async def test_other_fields_survive(provider):
    await _write(
        provider, "operator", SEEDED_AGENTS_V4["operator"],
        temperature=0.2, max_tool_turns=7,
    )

    await M034SeededAgentsV5().apply(provider)

    row = await provider.get_storage(AgentView).get("operator")
    assert row.system_prompt == _current("operator").system_prompt
    assert row.temperature == 0.2
    assert row.max_tool_turns == 7
    assert row.model.profile_id == "custom"


async def test_a_second_run_changes_nothing_and_warns_nothing(provider, caplog):
    for agent_id, definition in SEEDED_AGENTS_V4.items():
        await _write(provider, agent_id, definition)
    migration = M034SeededAgentsV5()
    await migration.apply(provider)
    agents = provider.get_storage(AgentView)
    before = {a: (await agents.get(a)).model_dump() for a in SEEDED_AGENT_FACTORIES}

    with caplog.at_level(logging.WARNING):
        await migration.apply(provider)

    after = {a: (await agents.get(a)).model_dump() for a in SEEDED_AGENT_FACTORIES}
    assert after == before
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_missing_agent_is_skipped(provider):
    await M034SeededAgentsV5().apply(provider)

    page = await provider.get_storage(AgentView).list(OffsetPage(offset=0, length=100))
    assert page.items == []


async def test_a_fresh_install_baselines_and_seeds_v5(provider):
    from primer.bootstrap.seed import ensure_seeded_agents
    from primer.model.model_profile import ModelProfile

    assert await run_migrations(provider, is_fresh_install=True) == LATEST_VERSION
    assert LATEST_VERSION >= 34
    await provider.get_storage(ModelProfile).create(
        ModelProfile(
            id="custom", description="default", provider_id="llm-1",
            model_name="qwen", context_length=32000,
        )
    )

    assert set(await ensure_seeded_agents(provider)) == set(SEEDED_AGENT_FACTORIES)

    for agent_id in SEEDED_AGENT_FACTORIES:
        await _assert_current(provider, agent_id)
    operator = await provider.get_storage(AgentView).get("operator")
    joined = "\n".join(operator.system_prompt)
    assert "runbooks/moderate-a-request" in joined
    assert "runbooks/workspace-status" in joined


def test_is_registered_as_version_34():
    assert isinstance(MIGRATIONS[33], M034SeededAgentsV5)
    assert MIGRATIONS[33].version == 34
