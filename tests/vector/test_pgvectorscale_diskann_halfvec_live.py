"""GI-30 on a live Postgres that has pgvectorscale: is ``USING diskann (vector halfvec_cosine_ops)`` an index pgvectorscale can build?

The review could not tell from the code (the opclass set is version-dependent). This asks the database once. Skipped where the ``vectorscale`` extension
is not installable (the lane's pgvector image has none). A FAILURE here confirms GI-30: refuse ``enable_diskann`` together with ``use_halfvec`` at save.
Gated on the single Postgres test gate (``tests/pg_gate.py``); ``tests/vector`` is a ``LANE_DIRS`` directory.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import asyncpg
import pytest

from tests.pg_gate import explicit_port, require_postgres_url

_WHAT = "the live pgvectorscale diskann halfvec check"
# No ``postgres`` marker: the lane image has no vectorscale, so its skip there is expected, not a lane failure.
pytestmark = pytest.mark.asyncio


async def test_diskann_can_index_a_halfvec_column() -> None:
    url = urlparse(require_postgres_url(_WHAT))
    conn = await asyncpg.connect(
        host=url.hostname or "localhost", port=explicit_port(url), user=url.username or "postgres", password=url.password or "",
        database=(url.path or "/postgres").lstrip("/") or "postgres",
    )
    schema = f"gi30{uuid.uuid4().hex[:12]}"
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_available_extensions WHERE name = 'vectorscale'"):
            pytest.skip("pgvectorscale is not installed on this database")
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vectorscale")
        await conn.execute(f'CREATE TABLE "{schema}".t (id int, vector halfvec(8))')
        try:
            await conn.execute(f'CREATE INDEX ON "{schema}".t USING diskann (vector halfvec_cosine_ops)')
        except asyncpg.UndefinedObjectError as exc:
            pytest.fail(f"GI-30 confirmed: this pgvectorscale has no halfvec opclass for diskann ({exc}); refuse enable_diskann with use_halfvec at save")
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
