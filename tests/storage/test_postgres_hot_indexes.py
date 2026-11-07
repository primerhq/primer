"""Hot-field expression-index DDL on the Postgres JSONB tables.

Two layers:

* Pure DDL-shape assertions (no database) -- the ``_HOT_FIELD_INDEXES``
  registry declares the expected indexes and ``_hot_field_index_ddl``
  renders the ``CREATE INDEX IF NOT EXISTS`` statements correctly.
* A live ``pg_indexes`` introspection (skipped unless
  ``PRIMER_TEST_POSTGRES_URL`` is set) proving the indexes are actually
  created on first handle use.

See ``primer/storage/postgres.py`` (``_HOT_FIELD_INDEXES``).
"""

from __future__ import annotations

import pytest

from tests.pg_gate import needs_postgres

from primer.storage.postgres import (
    _HOT_FIELD_INDEXES,
    _hot_field_index_ddl,
    _table_name_for,
)
from primer.storage.sqlite import _SQLITE_HOT_FIELD_INDEXES


# ---------------------------------------------------------------------------
# Pure DDL-shape assertions (no DB needed)
# ---------------------------------------------------------------------------


def test_registry_declares_the_hot_tables():
    """token_hash / unique username / session status / channel routing / useridentity
    uniqueness / webhook recovery / tool-approval-record gate key /
    artifact-location routing / document collection reads / the eight
    access-members GIN indexes are all registered."""
    assert set(_HOT_FIELD_INDEXES) == {
        "apitoken", "sessions", "channel", "useridentity",
        "webhookdelivery", "toolapprovalrecord", "artifactlocation", "document",
        "agent", "graph", "toolset", "workspacetemplate", "workspace", "externaltoolcall", "collection", "trigger", "harness",
        "modelprofile", "workspaceprovider", "channelprovider", "semanticsearchprovider", "embeddingprovider", "crossencoderprovider", "llmprovider",
        "serviceversion", "user", "service", "subscription",
        "toolcalltask",
    }


def _webhook_index(registry, suffix):
    (entry,) = [item for item in registry["webhookdelivery"] if item[0] == suffix]
    return entry


def test_webhookdelivery_indexes_cover_recovery_and_global_retention_order():
    assert _webhook_index(_HOT_FIELD_INDEXES, "status") == (
        "status",
        False,
        "((data->>'status'))",
    )
    suffix, unique, expr = _webhook_index(
        _HOT_FIELD_INDEXES, "terminal_completed_at_id"
    )
    assert suffix == "terminal_completed_at_id"
    assert unique is False
    assert expr.startswith("((data->>'completed_at'), id) WHERE ")
    assert "(data->>'completed_at') IS NOT NULL" in expr
    assert "status" not in expr

    assert _webhook_index(_SQLITE_HOT_FIELD_INDEXES, "status") == (
        "status",
        False,
        "(json_extract(data,'$.status'))",
    )
    sqlite_suffix, sqlite_unique, sqlite_expr = _webhook_index(
        _SQLITE_HOT_FIELD_INDEXES, "terminal_completed_at_id"
    )
    assert sqlite_suffix == suffix
    assert sqlite_unique is False
    assert sqlite_expr.startswith(
        "((json_extract(data,'$.completed_at') IS NULL), "
        "json_extract(data,'$.completed_at'), id) WHERE "
    )
    assert "json_extract(data,'$.completed_at') IS NOT NULL" in sqlite_expr
    assert "status" not in sqlite_expr


def test_webhookdelivery_index_ddl_matches_the_retention_order():
    statements = _hot_field_index_ddl(
        "webhookdelivery", '"public"."webhookdelivery"'
    )
    assert len(statements) == 2
    status, terminal = statements
    assert '"webhookdelivery_status"' in status
    assert "((data->>'status'))" in status
    assert '"webhookdelivery_terminal_completed_at_id"' in terminal
    assert "((data->>'completed_at'), id) WHERE" in terminal
    assert "(data->>'completed_at') IS NOT NULL" in terminal
    assert "status" not in terminal


async def test_sqlite_creates_the_webhook_recovery_and_retention_indexes(tmp_path):
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    provider = SqliteStorageProvider(
        SqliteConfig(path=str(tmp_path / "webhook.sqlite"))
    )
    await provider.initialize()
    try:
        from primer.model.webhook_delivery import WebhookDelivery

        await provider.get_storage(WebhookDelivery).get("__bootstrap__")
        cursor = await provider.connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'webhookdelivery'"
        )
        indexes = {name: sql for name, sql in await cursor.fetchall()}
    finally:
        await provider.aclose()

    assert "webhookdelivery_status" in indexes
    terminal = indexes["webhookdelivery_terminal_completed_at_id"]
    assert "(json_extract(data,'$.completed_at') IS NULL)" in terminal
    assert "json_extract(data,'$.completed_at'), id" in terminal
    assert "json_extract(data,'$.completed_at') IS NOT NULL" in terminal
    assert "status" not in terminal


async def test_sqlite_parameterized_retention_query_uses_the_order_index(tmp_path):
    from primer.model.provider import SqliteConfig
    from primer.model.storage import OffsetPage, OrderBy
    from primer.model.webhook_delivery import WebhookDelivery
    from primer.storage._sqlite_predicate import (
        _SqlitePredicateTranslator,
        render_order_by_sqlite,
    )
    from primer.storage.q import Q
    from primer.storage.sqlite import SqliteStorageProvider

    provider = SqliteStorageProvider(
        SqliteConfig(path=str(tmp_path / "webhook-plan.sqlite"))
    )
    await provider.initialize()
    try:
        await provider.get_storage(WebhookDelivery).get("__bootstrap__")
        await provider.connection.execute(
            "WITH RECURSIVE seq(i) AS ("
            "SELECT 1 UNION ALL SELECT i + 1 FROM seq WHERE i < 1000"
            ") INSERT INTO webhookdelivery(id, data) "
            "SELECT 'delivery-' || printf('%04d', i), "
            "json_object("
            "'status', CASE WHEN i % 2 = 0 THEN 'done' ELSE 'failed' END, "
            "'completed_at', printf('2026-01-01T00:00:00.%06dZ', i)"
            ") FROM seq"
        )
        await provider.connection.execute("ANALYZE webhookdelivery")
        predicate = (
            Q(WebhookDelivery)
            .where_in("status", ("done", "failed"))
            .where_not_null("completed_at")
            .build()
        )
        translator = _SqlitePredicateTranslator(WebhookDelivery)
        where_sql, params = translator.translate(predicate)
        order_sql = render_order_by_sqlite(
            WebhookDelivery,
            [OrderBy(field="completed_at", direction="asc")],
        )
        page = OffsetPage(offset=0, length=100, want_total=False)
        cursor = await provider.connection.execute(
            "EXPLAIN QUERY PLAN "
            'SELECT id, data FROM "webhookdelivery" '
            f"WHERE {where_sql} {order_sql} LIMIT ? OFFSET ?",
            [*params, page.length, page.offset],
        )
        plan = "\n".join(str(row[3]) for row in await cursor.fetchall())
    finally:
        await provider.aclose()

    assert "webhookdelivery_terminal_completed_at_id" in plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan


def test_webhook_delivery_model_maps_to_the_webhookdelivery_table():
    """The index lives on the same table the WebhookDelivery model stores in
    (the lowercased class name)."""
    from primer.model.webhook_delivery import WebhookDelivery

    assert _table_name_for(WebhookDelivery) == "webhookdelivery"
    assert "webhookdelivery" in _HOT_FIELD_INDEXES






def test_apitoken_token_hash_index_is_unique_on_the_scalar():
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["apitoken"]
    assert suffix == "token_hash_uniq"
    assert unique is True
    assert expr == "((data->>'token_hash'))"


def test_sessions_status_index_is_a_plain_btree_on_the_scalar():
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["sessions"][:1]
    assert suffix == "status"
    assert unique is False
    assert expr == "((data->>'status'))"


def test_sessions_workspace_id_index_is_a_plain_btree_on_the_scalar():
    """The cross-workspace list filters (access-control P2) push
    ``workspace_id IN <readable>`` into the sessions query; index-backed."""
    (suffix, unique, expr), = [
        e for e in _HOT_FIELD_INDEXES["sessions"] if e[0] == "workspace_id"
    ]
    assert suffix == "workspace_id"
    assert unique is False
    assert expr == "((data->>'workspace_id'))"


def test_document_collection_id_index_is_a_plain_btree_on_the_scalar():
    """Collection reads and the flat document lists (access-control P3) filter
    documents on ``collection_id``; index-backed."""
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["document"]
    assert suffix == "collection_id"
    assert unique is False
    assert expr == "((data->>'collection_id'))"


def test_channel_index_covers_provider_id_and_external_id():
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["channel"][:1]
    assert suffix == "provider_external"
    assert unique is False
    assert expr == "((data->>'provider_id'), (data->>'external_id'))"


def test_artifactlocation_index_covers_provider_id_counts():
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["artifactlocation"]
    assert suffix == "provider_id"
    assert unique is False
    assert expr == "((data->>'provider_id'))"
    (sqlite_suffix, sqlite_unique, sqlite_expr), = _SQLITE_HOT_FIELD_INDEXES[
        "artifactlocation"
    ]
    assert sqlite_suffix == "provider_id"
    assert sqlite_unique is False
    assert sqlite_expr == "(json_extract(data,'$.provider_id'))"


def test_tool_call_task_gate_route_index_matches_on_both_backends():
    assert _HOT_FIELD_INDEXES["toolcalltask"] == [(
        "gate_route",
        False,
        "((data->>'state'), (data->>'session_id'), "
        "(data->>'gate_occurrence_token'))",
    )]
    assert _SQLITE_HOT_FIELD_INDEXES["toolcalltask"] == [(
        "gate_route",
        False,
        "(json_extract(data,'$.state'), "
        "json_extract(data,'$.session_id'), "
        "json_extract(data,'$.gate_occurrence_token'))",
    )]


async def test_sqlite_creates_the_tool_call_task_gate_route_index(tmp_path):
    from primer.model.provider import SqliteConfig
    from primer.model.tool_call_task import ToolCallTask
    from primer.storage.sqlite import SqliteStorageProvider

    provider = SqliteStorageProvider(
        SqliteConfig(path=str(tmp_path / "task-gates.sqlite"))
    )
    await provider.initialize()
    try:
        await provider.get_storage(ToolCallTask).get("__bootstrap__")
        cursor = await provider.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'toolcalltask_gate_route'"
        )
        row = await cursor.fetchone()
    finally:
        await provider.aclose()

    assert row is not None
    assert "json_extract(data,'$.state')" in row[0]
    assert "json_extract(data,'$.session_id')" in row[0]
    assert "json_extract(data,'$.gate_occurrence_token')" in row[0]


def test_session_model_maps_to_the_sessions_table():
    """The status index lives on the same table the Session model stores in."""
    from primer.model.workspace_session import WorkspaceSession

    assert _table_name_for(WorkspaceSession) == "sessions"
    assert "sessions" in _HOT_FIELD_INDEXES


@pytest.mark.parametrize(
    ("table", "expected_names"),
    [
        ("apitoken", ["apitoken_token_hash_uniq"]),
        ("sessions", [
            "sessions_status", "sessions_workspace_id", "sessions_parent_session_id", "sessions_last_worker_id", "sessions_binding_agent_id",
            "sessions_binding_graph_id", "sessions_turn_status", "sessions_parked_status", "sessions_tool_wait_reconcile",
            "sessions_tool_wait_reconcile_v2", "sessions_created_at_desc",
        ]),
        ("channel", ["channel_provider_external", "channel_access_members"]),
        ("artifactlocation", ["artifactlocation_provider_id"]),
        ("document", ["document_collection_id"]),
        ("unindexed_table", []),
    ],
)
def test_hot_field_index_ddl_renders_create_if_not_exists(table, expected_names):
    qualified = f'"public"."{table}"'
    stmts = _hot_field_index_ddl(table, qualified)
    assert len(stmts) == len(expected_names)
    for stmt, name in zip(stmts, expected_names, strict=True):
        # Idempotent + plain (NOT concurrent -- it runs in the txn'd table
        # create path), targets this table, on the extracted JSONB scalar.
        assert "IF NOT EXISTS" in stmt
        assert "CONCURRENTLY" not in stmt
        assert f'"{name}"' in stmt
        assert qualified in stmt
        assert "data->" in stmt


def test_token_hash_ddl_emits_unique_index():
    (stmt,) = _hot_field_index_ddl("apitoken", '"public"."apitoken"')
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in stmt


def test_status_ddl_emits_plain_index():
    (stmt,) = [
        s for s in _hot_field_index_ddl("sessions", '"public"."sessions"')
        if '"sessions_status"' in s
    ]
    assert "CREATE INDEX IF NOT EXISTS" in stmt
    assert "UNIQUE" not in stmt


# ---------------------------------------------------------------------------
# Live pg_indexes introspection (gated on a real Postgres DSN)
# ---------------------------------------------------------------------------

pg = needs_postgres("Postgres hot-index tests")


async def _index_names(sp, table: str) -> set[str]:
    async with sp.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT indexname FROM pg_indexes
             WHERE schemaname = $1 AND tablename = $2
            """,
            sp.schema,
            table,
        )
    return {r["indexname"] for r in rows}


@pg
@pytest.mark.asyncio
async def test_hot_indexes_created_on_first_handle_use(postgres_storage_provider):
    """Touching each model's handle creates its hot-field index(es)."""
    from primer.model.api_token import ApiToken
    from primer.model.artifact import ArtifactLocation
    from primer.model.channel import Channel
    from primer.model.collection import Document
    from primer.model.workspace_session import WorkspaceSession

    sp = postgres_storage_provider
    # First handle use runs _ensure_table -> creates table + GIN + hot indexes.
    await sp.get_storage(ApiToken).get("__bootstrap__")
    await sp.get_storage(WorkspaceSession).get("__bootstrap__")
    await sp.get_storage(Channel).get("__bootstrap__")
    await sp.get_storage(ArtifactLocation).get("__bootstrap__")
    await sp.get_storage(Document).get("__bootstrap__")

    assert "apitoken_token_hash_uniq" in await _index_names(sp, "apitoken")
    assert "sessions_status" in await _index_names(sp, "sessions")
    assert "sessions_workspace_id" in await _index_names(sp, "sessions")
    assert "sessions_tool_wait_reconcile_v2" in await _index_names(
        sp, "sessions",
    )
    assert "channel_provider_external" in await _index_names(sp, "channel")
    assert "artifactlocation_provider_id" in await _index_names(sp, "artifactlocation")
    assert "document_collection_id" in await _index_names(sp, "document")


@pg
@pytest.mark.asyncio
async def test_terminal_tool_wait_query_uses_marker_and_id_order_index(postgres_storage_provider):
    from primer.claim.tool_wait_reconcile import (
        _terminal_tool_wait_candidate_predicate,
    )
    from primer.model.workspace_session import WorkspaceSession
    from primer.storage._predicate import render_order_by

    sp = postgres_storage_provider
    store = sp.get_storage(WorkspaceSession)
    await store.get("__bootstrap__")
    predicate = _terminal_tool_wait_candidate_predicate()
    translator = store._translator()
    where_sql = await store._where(translator, predicate)
    assert "data->>'parked_tool_batches' IS NOT NULL" in where_sql
    assert "data->>'parked_status' = ANY($1::text[])" in where_sql
    assert translator._params == [["parked", "resumable"]]
    limit = translator.append_param(201)
    sql = (
        f"EXPLAIN SELECT id, data FROM {store._qualified} "
        f"WHERE {where_sql} {render_order_by(WorkspaceSession, None)} LIMIT {limit}"
    )
    async with sp.pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off")
        rows = await conn.fetch(sql, *translator._params)
    plan = " ".join(row[0] for row in rows)
    assert "sessions_tool_wait_reconcile_v2" in plan
    assert "Sort" not in plan


@pytest.mark.parametrize("table", ["toolapprovalrecord", "externaltoolcall"])
def test_the_cross_workspace_lists_have_a_workspace_id_index(table):
    """The records and external call lists push ``workspace_id IN <readable>`` into the query (access-control P2 Task 5); index-backed."""
    (suffix, unique, expr), = (e for e in _HOT_FIELD_INDEXES[table] if e[0] == "workspace_id")
    assert (suffix, unique, expr) == ("workspace_id", False, "((data->>'workspace_id'))")
