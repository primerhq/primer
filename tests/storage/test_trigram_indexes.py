"""L3 / Decision 11: trigram indexes for the `?q=` search, created only when pg_trgm is available, never failing a boot."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import primer.storage.postgres as pg
from primer.model.agent import Agent
from primer.model.model_profile import ModelProfile
from primer.model.service import Service
from primer.storage._predicate import _render_field_expr


def test_each_index_is_on_the_expression_the_ilike_predicate_renders():
    for model, table in ((ModelProfile, "modelprofile"), (Service, "service"), (Agent, "agent")):
        for field in pg._TRIGRAM_FIELDS[table]:
            ddl = pg._trigram_index_ddl(table, '"s"."t"', field)
            assert f"(({_render_field_expr(model, field)}) gin_trgm_ops)" in ddl and "IF NOT EXISTS" in ddl and "CONCURRENTLY" not in ddl


class _Conn:
    def __init__(self, extension_error=None):
        self.statements: list[str] = []
        self.extension_error = extension_error

    async def execute(self, sql, *args):
        self.statements.append(sql)
        if "CREATE EXTENSION" in sql and self.extension_error:
            raise self.extension_error


def _handle(conn, table="modelprofile"):
    @asynccontextmanager
    async def acquire():
        yield conn

    handle = pg.PostgresStorage.__new__(pg.PostgresStorage)
    handle._provider = SimpleNamespace(pool=SimpleNamespace(acquire=acquire))
    handle._table, handle._qualified = table, f'"s"."{table}"'
    return handle


async def test_the_indexes_are_created_after_the_extension():
    conn = _Conn()
    await _handle(conn)._ensure_trigram_indexes()
    assert conn.statements[0] == "CREATE EXTENSION IF NOT EXISTS pg_trgm"
    assert len([s for s in conn.statements if "gin_trgm_ops" in s]) == 3


async def test_a_role_that_may_not_create_the_extension_skips_the_indexes_logs_once_and_does_not_raise(caplog, monkeypatch):
    monkeypatch.setattr(pg, "_trigram_unavailable_logged", False)
    conn = _Conn(extension_error=PermissionError("permission denied to create extension"))
    with caplog.at_level(logging.WARNING):
        await _handle(conn)._ensure_trigram_indexes()
        await _handle(conn, "agent")._ensure_trigram_indexes()
    assert not [s for s in conn.statements if "gin_trgm_ops" in s]
    assert len([r for r in caplog.records if "pg_trgm" in r.getMessage()]) == 1


async def test_a_failing_index_statement_never_raises(caplog):
    class _Bad(_Conn):
        async def execute(self, sql, *args):
            await super().execute(sql, *args)
            if "gin_trgm_ops" in sql:
                raise RuntimeError("boom")

    await _handle(_Bad())._ensure_trigram_indexes()


async def test_a_table_with_no_searchable_fields_does_nothing():
    conn = _Conn()
    await _handle(conn, "sessions")._ensure_trigram_indexes()
    assert conn.statements == []
