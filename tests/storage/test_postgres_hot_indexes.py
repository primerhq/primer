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

import os

import pytest

from primer.storage.postgres import (
    _HOT_FIELD_INDEXES,
    _hot_field_index_ddl,
    _table_name_for,
)


# ---------------------------------------------------------------------------
# Pure DDL-shape assertions (no DB needed)
# ---------------------------------------------------------------------------


def test_registry_declares_the_hot_tables():
    """token_hash / session status / chat recovery / channel routing /
    useridentity uniqueness / webhook recovery are all registered."""
    assert set(_HOT_FIELD_INDEXES) == {
        "apitoken", "sessions", "chat", "channel", "useridentity",
        "webhookdelivery",
    }


def test_webhookdelivery_index_covers_the_recovery_status_filter():
    """Startup webhook recovery filters status='pending' on every boot; an
    expression index on the extracted scalar serves that."""
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["webhookdelivery"]
    assert suffix == "status"
    assert unique is False
    assert expr == "((data->>'status'))"


def test_webhook_delivery_model_maps_to_the_webhookdelivery_table():
    """The index lives on the same table the WebhookDelivery model stores in
    (the lowercased class name)."""
    from primer.model.webhook_delivery import WebhookDelivery

    assert _table_name_for(WebhookDelivery) == "webhookdelivery"
    assert "webhookdelivery" in _HOT_FIELD_INDEXES


def test_chat_index_covers_status_and_turn_status():
    """Startup chat recovery filters status='active' AND turn_status IN
    (claimable, running); a composite expression index serves that."""
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["chat"]
    assert suffix == "status_turn"
    assert unique is False
    assert expr == "((data->>'status'), (data->>'turn_status'))"


def test_chat_model_maps_to_the_chat_table():
    """The chat index lives on the same table the Chat model stores in
    (singular 'chat', the lowercased class name)."""
    from primer.model.chats import Chat

    assert _table_name_for(Chat) == "chat"
    assert "chat" in _HOT_FIELD_INDEXES


def test_apitoken_token_hash_index_is_unique_on_the_scalar():
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["apitoken"]
    assert suffix == "token_hash_uniq"
    assert unique is True
    assert expr == "((data->>'token_hash'))"


def test_sessions_status_index_is_a_plain_btree_on_the_scalar():
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["sessions"]
    assert suffix == "status"
    assert unique is False
    assert expr == "((data->>'status'))"


def test_channel_index_covers_provider_id_and_external_id():
    (suffix, unique, expr), = _HOT_FIELD_INDEXES["channel"]
    assert suffix == "provider_external"
    assert unique is False
    assert expr == "((data->>'provider_id'), (data->>'external_id'))"


def test_session_model_maps_to_the_sessions_table():
    """The status index lives on the same table the Session model stores in."""
    from primer.model.workspace_session import WorkspaceSession

    assert _table_name_for(WorkspaceSession) == "sessions"
    assert "sessions" in _HOT_FIELD_INDEXES


@pytest.mark.parametrize(
    ("table", "expected_names"),
    [
        ("apitoken", ["apitoken_token_hash_uniq"]),
        ("sessions", ["sessions_status"]),
        ("channel", ["channel_provider_external"]),
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
        assert "data->>" in stmt


def test_token_hash_ddl_emits_unique_index():
    (stmt,) = _hot_field_index_ddl("apitoken", '"public"."apitoken"')
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in stmt


def test_status_ddl_emits_plain_index():
    (stmt,) = _hot_field_index_ddl("sessions", '"public"."sessions"')
    assert "CREATE INDEX IF NOT EXISTS" in stmt
    assert "UNIQUE" not in stmt


# ---------------------------------------------------------------------------
# Live pg_indexes introspection (gated on a real Postgres DSN)
# ---------------------------------------------------------------------------

pg = pytest.mark.skipif(
    not os.environ.get("PRIMER_TEST_POSTGRES_URL"),
    reason="needs PRIMER_TEST_POSTGRES_URL set",
)


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
    from primer.model.channel import Channel
    from primer.model.workspace_session import WorkspaceSession

    sp = postgres_storage_provider
    # First handle use runs _ensure_table -> creates table + GIN + hot indexes.
    await sp.get_storage(ApiToken).get("__bootstrap__")
    await sp.get_storage(WorkspaceSession).get("__bootstrap__")
    await sp.get_storage(Channel).get("__bootstrap__")

    assert "apitoken_token_hash_uniq" in await _index_names(sp, "apitoken")
    assert "sessions_status" in await _index_names(sp, "sessions")
    assert "channel_provider_external" in await _index_names(sp, "channel")
