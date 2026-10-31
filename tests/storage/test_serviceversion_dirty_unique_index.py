"""ensure_table must survive the (service_id, version) duplicates the
pre-fix publish race left behind (KN-19j follow-up).

The unique (service_id, version) hot-field index is created inside
ensure_table. On an existing database that already holds duplicate
rows -- exactly the race KN-19j describes -- CREATE UNIQUE INDEX
fails; unhandled, the raised error breaks every ServiceVersion read
and write for good. Creating THIS index must instead log one warning
naming the index and the duplicates, then continue without it (the
publish retry still works; it simply cannot detect the race). Every
other hot-field index keeps raising.
"""

from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import parse_qs, urlparse

import aiosqlite
import asyncpg
import pytest

from primer.model.common import dump_for_storage
from primer.model.except_ import ConfigError
from primer.model.provider import (
    PoolConfig,
    PostgresConfig,
    SqliteConfig,
)
from primer.model.service import ServiceManifest, ServiceVersion
from primer.model.storage import OffsetPage
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import (
    CANONICAL_ENV,
    explicit_port,
    needs_postgres,
    require_postgres_url,
)

INDEX_NAME = "serviceversion_service_version_uniq"
WHAT = "the dirty serviceversion unique-index tests"


def _duplicate_rows() -> list[tuple[str, str]]:
    """Two ServiceVersion rows sharing (service_id, version) -- the shape
    the pre-fix publish race left on existing databases. The ids differ;
    only the (service_id, version) pair repeats."""
    rows: list[tuple[str, str]] = []
    for row_id in ("sv-dirty-1", "sv-dirty-2"):
        version = ServiceVersion(
            id=row_id,
            service_id="svc-dirty",
            version=1,
            manifest=ServiceManifest(),
            files={},
        )
        dumped = dump_for_storage(version)
        dumped.pop("id")
        rows.append((row_id, json.dumps(dumped)))
    return rows


# ---- SQLite -----------------------------------------------------------------


async def test_ensure_table_tolerates_duplicate_versions_sqlite(
    tmp_path, caplog,
):
    """Pre-seed two ServiceVersion rows with the same (service_id,
    version), then ensure_table must succeed, reads must work, and one
    warning must name the index. (Pre-fix red: CREATE UNIQUE INDEX over
    the duplicates raises sqlite3.IntegrityError inside ensure_table
    and the list never runs.)"""
    path = tmp_path / "dirty.sqlite"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            'CREATE TABLE "serviceversion" ('
            "id TEXT PRIMARY KEY, "
            "data TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        await db.executemany(
            'INSERT INTO "serviceversion" (id, data) VALUES (?, ?)',
            _duplicate_rows(),
        )
        await db.commit()

    sp = SqliteStorageProvider(SqliteConfig(path=str(path)))
    await sp.initialize()
    try:
        with caplog.at_level(logging.WARNING, logger="primer.storage.sqlite"):
            storage = sp.get_storage(ServiceVersion)
            page = await storage.list(OffsetPage(offset=0, length=50))

        # ensure_table succeeded and the dirty rows are readable
        assert sorted(item.id for item in page.items) == [
            "sv-dirty-1", "sv-dirty-2",
        ]
        # exactly one warning, naming the index
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and INDEX_NAME in r.getMessage()
        ]
        assert len(warnings) == 1
    finally:
        await sp.aclose()


# ---- Postgres (gated) ---------------------------------------------------------


def _pg_url_parts():
    url = require_postgres_url(WHAT)
    p = urlparse(url)
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {CANONICAL_ENV}")
    return p


@needs_postgres(WHAT)
async def test_ensure_table_tolerates_duplicate_versions_postgres(caplog):
    """The Postgres equivalent of the SQLite test above. A failed
    statement aborts the whole Postgres transaction, so the fix must
    create this one index under a savepoint: duplicate rows -> roll back
    to the savepoint, warn once, commit the table and continue."""
    p = _pg_url_parts()
    database = (p.path or "/postgres").lstrip("/") or "postgres"
    base_schema = parse_qs(p.query).get("schema", ["public"])[0]
    schema = f"{base_schema}_svdirty_{uuid.uuid4().hex[:8]}"

    conn = await asyncpg.connect(
        host=p.hostname or "localhost",
        port=explicit_port(p),
        user=p.username or "postgres",
        password=p.password or "",
        database=database,
    )
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        await conn.execute(
            f'CREATE TABLE "{schema}".serviceversion ('
            "id text PRIMARY KEY, "
            "data jsonb NOT NULL, "
            "created_at timestamptz NOT NULL DEFAULT now(), "
            "updated_at timestamptz NOT NULL DEFAULT now()"
            ")"
        )
        for row_id, data in _duplicate_rows():
            await conn.execute(
                f'INSERT INTO "{schema}".serviceversion (id, data) '
                "VALUES ($1, $2::jsonb)",
                row_id,
                data,
            )

        sp = PostgresStorageProvider(
            PostgresConfig(
                hostname=p.hostname or "localhost",
                port=explicit_port(p),
                username=p.username or "postgres",
                password=p.password or "",
                database=database,
                db_schema=schema,
                pool=PoolConfig(min_size=1, max_size=2),
            )
        )
        await sp.initialize()
        try:
            with caplog.at_level(
                logging.WARNING, logger="primer.storage.postgres",
            ):
                storage = sp.get_storage(ServiceVersion)
                page = await storage.list(OffsetPage(offset=0, length=50))

            assert sorted(item.id for item in page.items) == [
                "sv-dirty-1", "sv-dirty-2",
            ]
            warnings = [
                r for r in caplog.records
                if r.levelno == logging.WARNING and INDEX_NAME in r.getMessage()
            ]
            assert len(warnings) == 1
        finally:
            await sp.aclose()
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()
