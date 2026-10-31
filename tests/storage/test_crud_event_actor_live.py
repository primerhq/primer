"""The Postgres CRUD event writer names the acting person (access-control plan P5 Task 1, Decision 9): the twin of
``tests/events/test_event_attribution.py``'s SQLite cases.

A create, update and delete under ``acting_as(person)`` append events whose actor is ``user:<user id>``; with nobody acting, or the system person,
the actor is ``system``. Gate: ``PRIMER_TEST_POSTGRES_URL`` (see ``tests/pg_gate.py``); the schema is a throwaway one, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import ClassVar
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from primer.access.acting import acting_as
from primer.access.person import Person
from primer.events.registry import register_event_kind
from primer.model.common import Identifiable
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_WHAT = "the CRUD event actor tests"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]
_BOUND_S = 60.0


class EvActorLiveRow(Identifiable):
    _id_prefix: ClassVar[str | None] = "evactorlive"
    name: str = ""


@pytest_asyncio.fixture
async def sp() -> AsyncIterator[PostgresStorageProvider]:
    register_event_kind("evactorlive", EvActorLiveRow)
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"evactor{uuid.uuid4().hex[:14]}"
    provider = PostgresStorageProvider(PostgresConfig(
        hostname=url.hostname or "localhost",
        port=explicit_port(url),
        username=url.username or "primer",
        password=url.password or "",  # type: ignore[arg-type]
        database=(url.path or "/").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=2),
    ))
    async with asyncio.timeout(_BOUND_S):
        await provider.initialize()
        await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        async with asyncio.timeout(_BOUND_S):
            async with provider.pool.acquire() as connection:
                await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await provider.aclose()


async def test_crud_events_name_the_acting_person(sp):
    storage = sp.get_storage(EvActorLiveRow)
    with acting_as(Person("u-live", "user")):
        row = await storage.create(EvActorLiveRow(id="r1", name="one"))
        row.name = "two"
        await storage.update(row)
        await storage.patch_if("r1", {"name": "three"}, where={"name": ["two"]})
        await storage.delete("r1")
    await storage.create(EvActorLiveRow(id="r2", name="nobody acting"))
    with acting_as(Person(None, "system")):
        await storage.create(EvActorLiveRow(id="r3", name="the system person"))

    events = [(e.entity_id, e.event_type, e.actor) for e in await sp.get_event_store().read_after(0) if e.entity_kind == "evactorlive"]
    assert [(t, a) for i, t, a in events if i == "r1"] == [
        ("evactorlive.created", "user:u-live"), ("evactorlive.updated", "user:u-live"), ("evactorlive.updated", "user:u-live"),
        ("evactorlive.deleted", "user:u-live"),
    ]
    assert [(i, a) for i, _, a in events if i != "r1"] == [("r2", "system"), ("r3", "system")]
