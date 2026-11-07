"""M2: the hot fields of sessions, subscriptions, identities and the event log are indexed, on the expression the queries use."""

from __future__ import annotations

import pytest

from primer.model.oidc import UserIdentity
from primer.model.storage import OffsetPage
from primer.model.trigger import Subscription
from primer.model.workspace_session import WorkspaceSession
from primer.storage._predicate import _render_field_expr
from primer.storage.postgres import _HOT_FIELD_INDEXES, _hot_field_index_ddl
from primer.storage.sqlite import SqliteConfig, SqliteStorageProvider, _SQLITE_HOT_FIELD_INDEXES

SESSION_FIELDS = {
    "parent_session_id": "parent_session_id",
    "last_worker_id": "last_worker_id",
    "binding_agent_id": "binding.agent_id",
    "binding_graph_id": "binding.graph_id",
    "turn_status": "turn_status",
    "parked_status": "parked_status",
}


def _entry(table: str, suffix: str):
    (found,) = [e for e in _HOT_FIELD_INDEXES[table] if e[0] == suffix]
    return found


@pytest.mark.parametrize("suffix,field", SESSION_FIELDS.items())
def test_each_session_index_is_on_the_expression_the_predicate_translator_emits(suffix, field):
    """An expression index only serves a query written with the same expression."""
    _, unique, expr = _entry("sessions", suffix)
    assert unique is False
    assert expr == f"(({_render_field_expr(WorkspaceSession, field)}))"


def test_subscription_and_identity_indexes_match_their_predicates():
    assert _entry("subscription", "trigger_id")[2] == f"(({_render_field_expr(Subscription, 'trigger_id')}))"
    assert _entry("useridentity", "user_id")[2] == f"(({_render_field_expr(UserIdentity, 'user_id')}))"


def test_the_created_at_index_matches_the_default_list_order():
    from primer.model.storage import OrderBy
    from primer.storage._predicate import render_order_by

    order = render_order_by(WorkspaceSession, [OrderBy(field="created_at", direction="desc")])
    assert order == "ORDER BY data->>'created_at' DESC NULLS LAST, id ASC"
    assert _entry("sessions", "created_at_desc")[2] == "((data->>'created_at') DESC NULLS LAST, id)"


def test_the_ddl_is_idempotent_and_plain():
    for table in ("sessions", "subscription", "useridentity"):
        for stmt in _hot_field_index_ddl(table, f'"s"."{table}"'):
            assert "IF NOT EXISTS" in stmt and "CONCURRENTLY" not in stmt


def test_terminal_tool_wait_index_matches_the_bounded_scan() -> None:
    from primer.claim.tool_wait_reconcile import (
        _terminal_tool_wait_candidate_predicate,
    )
    from primer.storage._predicate import _PredicateTranslator

    _, unique, pg_expr = _entry("sessions", "tool_wait_reconcile")
    assert unique is False
    assert pg_expr == "((data->>'parked_tool_batches'), id)"
    _, v2_unique, pg_v2_expr = _entry("sessions", "tool_wait_reconcile_v2")
    assert v2_unique is False
    assert pg_v2_expr == (
        "(id) WHERE (data->>'parked_tool_batches' IS NOT NULL)"
    )
    (suffix, sqlite_unique, sqlite_expr), = [
        entry for entry in _SQLITE_HOT_FIELD_INDEXES["sessions"]
        if entry[0] == "tool_wait_reconcile"
    ]
    assert suffix == "tool_wait_reconcile" and sqlite_unique is False
    assert sqlite_expr == "(json_extract(data,'$.parked_tool_batches'), id)"
    (v2_suffix, sqlite_v2_unique, sqlite_v2_expr), = [
        entry for entry in _SQLITE_HOT_FIELD_INDEXES["sessions"]
        if entry[0] == "tool_wait_reconcile_v2"
    ]
    assert v2_suffix == "tool_wait_reconcile_v2" and sqlite_v2_unique is False
    assert sqlite_v2_expr == (
        "(id, data) "
        "WHERE (json_extract(data,'$.parked_tool_batches') IS NOT NULL)"
    )

    predicate = _terminal_tool_wait_candidate_predicate()
    pg_sql, pg_params = _PredicateTranslator(WorkspaceSession).translate(predicate)
    assert "data->>'parked_tool_batches' IS NOT NULL" in pg_sql
    assert "data->>'parked_status' = ANY($1::text[])" in pg_sql
    assert pg_params == [["parked", "resumable"]]


@pytest.mark.asyncio
async def test_sqlite_creates_the_same_hot_fields_and_survives_a_second_boot(tmp_path):
    path = str(tmp_path / "m2.sqlite")
    for _ in range(2):  # the second boot runs every CREATE INDEX IF NOT EXISTS again
        sp = SqliteStorageProvider(SqliteConfig(path=path))
        await sp.initialize()
        for model in (WorkspaceSession, Subscription, UserIdentity):
            await sp.get_storage(model).list(OffsetPage(offset=0, length=1))
        await sp.get_event_store().ensure_schema()
        async with sp.connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'") as cur:
            names = {row[0] for row in await cur.fetchall()}
        await sp.aclose()
    expected = {f"{table}_{suffix}" for table in ("sessions", "subscription", "useridentity") for suffix, _, _ in _SQLITE_HOT_FIELD_INDEXES[table]}
    assert expected <= names
    assert {"events_session_id", "events_workspace_id"} <= names
    assert {f"sessions_{s}" for s in SESSION_FIELDS} <= names


@pytest.mark.asyncio
async def test_sqlite_session_find_by_an_indexed_field_returns_the_same_rows(tmp_path):
    from datetime import datetime, timezone

    from primer.model.storage import FieldRef, Op, Predicate, Value
    from primer.model.workspace_session import AgentSessionBinding, SessionStatus

    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "q.sqlite")))
    await sp.initialize()
    store = sp.get_storage(WorkspaceSession)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, agent in enumerate(["a", "b", "a"]):
        await store.create(WorkspaceSession(
            id=f"s{i}", workspace_id="w", binding=AgentSessionBinding(agent_id=agent), status=SessionStatus.RUNNING, created_at=now,
        ))
    found = await store.find(
        Predicate(left=FieldRef(name="binding.agent_id"), op=Op.EQ, right=Value(value="a")), OffsetPage(offset=0, length=10),
    )
    assert sorted(r.id for r in found.items) == ["s0", "s2"]
    await sp.aclose()


@pytest.mark.asyncio
async def test_sqlite_tool_wait_reconcile_query_uses_the_id_order_index(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "reconcile.sqlite")))
    try:
        await sp.initialize()
        from datetime import datetime, timezone

        from primer.model.storage import OffsetPage
        from primer.model.workspace_session import AgentSessionBinding, SessionStatus
        from primer.claim.tool_wait_reconcile import (
            _terminal_tool_wait_candidate_predicate,
        )
        from primer.storage._sqlite_predicate import render_order_by_sqlite

        await sp.connection.execute(
            'CREATE TABLE "sessions" ('
            "id TEXT PRIMARY KEY, data TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        await sp.connection.execute(
            'CREATE INDEX "sessions_tool_wait_reconcile" ON "sessions" '
            "(json_extract(data,'$.parked_tool_batches'), id)"
        )
        await sp.connection.commit()
        store = sp.get_storage(WorkspaceSession)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for session_id, marker in (("marked", True), ("cleared", None)):
            await store.create(WorkspaceSession(
                id=session_id,
                workspace_id="w",
                binding=AgentSessionBinding(agent_id="a"),
                status=SessionStatus.RUNNING,
                parked_status="parked",
                parked_tool_batches=marker,
                created_at=now,
            ))
        await sp.connection.execute(
            "WITH RECURSIVE seq(i) AS ("
            "SELECT 1 UNION ALL SELECT i + 1 FROM seq WHERE i < 1000"
            ") INSERT INTO sessions(id, data) "
            "SELECT 'cleared-' || printf('%04d', i), "
            "json_object("
            "'workspace_id', 'w', "
            "'binding', json_object('type', 'agent', 'agent_id', 'a'), "
            "'status', 'running', "
            "'parked_status', 'parked', "
            "'parked_tool_batches', NULL, "
            "'created_at', '2026-01-01T00:00:00Z'"
            ") FROM seq"
        )
        await sp.connection.execute(
            "INSERT INTO sessions(id, data) VALUES (?, json_object("
            "'workspace_id', 'w', "
            "'binding', json_object('type', 'agent', 'agent_id', 'a'), "
            "'status', 'running', "
            "'parked_status', 'corrupt', "
            "'parked_tool_batches', json('true'), "
            "'created_at', '2026-01-01T00:00:00Z'))",
            ("corrupt-status",),
        )
        await sp.connection.execute("ANALYZE sessions")
        await sp.connection.commit()
        predicate = _terminal_tool_wait_candidate_predicate()
        found = await store.find(
            predicate, OffsetPage(offset=0, length=10, want_total=False),
        )
        assert [row.id for row in found.items] == ["marked"]
        translator = store._translator()
        where_sql = await store._where(translator, predicate)
        assert "IS NOT NULL" in where_sql
        assert translator._params == ["parked", "resumable"]
        translator.append_param(201)
        sql = (
            'EXPLAIN QUERY PLAN SELECT id, data FROM "sessions" '
            f"WHERE {where_sql} {render_order_by_sqlite(WorkspaceSession, None)} LIMIT ?"
        )
        async with sp.connection.execute(sql, list(translator._params)) as cur:
            plan = " ".join(str(value) for row in await cur.fetchall() for value in row)
        assert "sessions_tool_wait_reconcile_v2" in plan
        assert "USE TEMP B-TREE FOR ORDER BY" not in plan
        async with sp.connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE name IN (?, ?) ORDER BY name",
            ("sessions_tool_wait_reconcile", "sessions_tool_wait_reconcile_v2"),
        ) as cur:
            indexes = dict(await cur.fetchall())
        assert set(indexes) == {
            "sessions_tool_wait_reconcile",
            "sessions_tool_wait_reconcile_v2",
        }
        assert "parked_tool_batches'), id" in indexes["sessions_tool_wait_reconcile"]
        assert "(id, data)" in indexes["sessions_tool_wait_reconcile_v2"]
    finally:
        await sp.aclose()


# ---------------------------------------------------------------------------
# Live Postgres (gated): the indexes exist and the filtered lookups use them
# ---------------------------------------------------------------------------

from tests.pg_gate import needs_postgres  # noqa: E402

pg = needs_postgres("Postgres M2 index tests")


@pg
@pytest.mark.asyncio
async def test_the_new_indexes_exist_and_events_are_indexed(postgres_storage_provider):
    sp = postgres_storage_provider
    for model in (WorkspaceSession, Subscription, UserIdentity):
        await sp.get_storage(model).get("__bootstrap__")
    await sp.get_event_store().ensure_schema()
    async with sp.pool.acquire() as conn:
        rows = await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = $1", sp.schema)
    names = {r["indexname"] for r in rows}
    assert {f"sessions_{s}" for s in [*SESSION_FIELDS, "created_at_desc"]} <= names
    assert {"subscription_trigger_id", "useridentity_user_id", "events_session_id", "events_workspace_id", "events_type_pattern"} <= names


@pg
@pytest.mark.asyncio
async def test_a_binding_agent_lookup_does_not_seq_scan_a_seeded_sessions_table(postgres_storage_provider):
    sp = postgres_storage_provider
    await sp.get_storage(WorkspaceSession).get("__bootstrap__")
    async with sp.pool.acquire() as conn:
        await conn.execute(
            f'INSERT INTO "{sp.schema}".sessions (id, data) '
            "SELECT 'x' || g, jsonb_build_object('id', 'x' || g, 'binding', jsonb_build_object('agent_id', 'a' || (g % 500))) "
            "FROM generate_series(1, 20000) g ON CONFLICT DO NOTHING"
        )
        await conn.execute(f'ANALYZE "{sp.schema}".sessions')
        await conn.execute("SET enable_seqscan = off")  # the index must be USABLE for the planner to pick it at all
        plan = "\n".join(
            r[0] for r in await conn.fetch(
                f"EXPLAIN SELECT id FROM \"{sp.schema}\".sessions WHERE data->'binding'->>'agent_id' = 'a7'"
            )
        )
    assert "sessions_binding_agent_id" in plan, plan
