"""The OAuth store across two connection pools on a live Postgres (plan 07, Task 5).

Two server processes share one database. Each has its own pool, which is what two ``PostgresStorageProvider`` instances on one schema are here, and a
``McpOAuthStore`` over each. The in-memory fake never really overlaps two awaits and SQLite has one connection, so only this file can show the properties the
sign-in rests on when the callers are different connections:

* a pending sign-in is given to exactly ONE of any number of concurrent ``take_pending`` calls, whichever pool they come from;
* two connects racing for the token row never write one version twice;
* of two pools patching the refresh lease with the same guard, exactly one applies, and the lease can be released with the guard its holder read back
  (the stored timestamp is in the form ``raw_generation`` reads, on the real backend);
* the table holds ciphertext only.

Gate: ``PRIMER_TEST_POSTGRES_URL`` (see ``tests/pg_gate.py``); the schema is a throwaway one, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from primer.common.secret_box import SecretBox
from primer.model.mcp_oauth import McpOAuthClient, McpOAuthPendingSignIn, McpOAuthTokenSet
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage import postgres as pg_backend
from primer.storage import raw_generation
from primer.storage.postgres import PostgresStorageProvider
from primer.toolset.oauth.store import McpOAuthStore, pending_key
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_WHAT = "the MCP OAuth store race tests"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
KEY = "live-store-key-0123456789"
#: Every wait is bounded: a call that never returns fails the test instead of hanging the lane.
_BOUND_S = 60.0


def _provider(url, schema: str) -> PostgresStorageProvider:
    return PostgresStorageProvider(PostgresConfig(
        hostname=url.hostname or "localhost",
        port=explicit_port(url),
        username=url.username or "primer",
        password=url.password or "",  # type: ignore[arg-type]
        database=(url.path or "/").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    ))


@pytest_asyncio.fixture
async def pools() -> AsyncIterator[tuple[PostgresStorageProvider, PostgresStorageProvider]]:
    """Two providers (two pools) on one throwaway schema of the gated database, dropped afterwards."""
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"mcpoauth{uuid.uuid4().hex[:14]}"
    first, second = _provider(url, schema), _provider(url, schema)
    async with asyncio.timeout(_BOUND_S):
        await first.initialize()
        await second.initialize()
    try:
        yield first, second
    finally:
        async with asyncio.timeout(_BOUND_S):
            async with first.pool.acquire() as connection:
                await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            for provider in (first, second):
                # The "table ensured" cache is keyed by id(provider): a later provider at this address must not skip its CREATE.
                for key in [k for k in pg_backend._table_ensured if k[0] == id(provider)]:
                    pg_backend._table_ensured.discard(key)
                await provider.aclose()


@pytest.fixture
def stores(pools) -> tuple[McpOAuthStore, McpOAuthStore]:
    box = SecretBox.from_key_material(KEY)
    return McpOAuthStore(pools[0], box), McpOAuthStore(pools[1], box)


def _token_row(store: McpOAuthStore, **over) -> McpOAuthTokenSet:
    base = {
        "id": "kb",
        "grant": "authorization_code",
        "access_token_enc": store.seal("tok-plain"),
        "refresh_token_enc": store.seal("ref-plain"),
        "expires_at": NOW + timedelta(hours=1),
        "token_endpoint": "https://as.example/token",
        "client_id": "client-1",
        "client_source": "registered",
        "config_fingerprint": "f" * 64,
        "connected_at": NOW,
    }
    base.update(over)
    return McpOAuthTokenSet(**base)


def _pending_row(store: McpOAuthStore, state: str) -> McpOAuthPendingSignIn:
    return McpOAuthPendingSignIn(
        id=state, toolset_id="kb", code_verifier_enc=store.seal("verifier-plain"), redirect_uri="https://p.example/cb", issuer="https://as.example",
        token_endpoint="https://as.example/token", client_id="client-1", client_source="registered", config_fingerprint="f" * 64,
        created_at=NOW, expires_at=NOW + timedelta(minutes=10),
    )


async def test_a_pending_sign_in_goes_to_exactly_one_taker_across_two_pools(stores):
    a, b = stores
    states = [f"state-{n}" for n in range(15)]
    async with asyncio.timeout(_BOUND_S):
        for state in states:
            await a.put_pending(_pending_row(a, state))
        takes = [(a if n % 2 else b).take_pending(state, now=NOW) for state in states for n in range(6)]
        results = await asyncio.gather(*takes)
    taken = sorted(row.id for row in results if row is not None)
    assert taken == sorted(pending_key(state) for state in states), "every state was given out exactly once"


async def test_an_expired_pending_sign_in_is_not_given_to_either_pool(stores):
    a, b = stores
    async with asyncio.timeout(_BOUND_S):
        await a.put_pending(_pending_row(a, "late"))
        later = NOW + timedelta(minutes=11)
        results = await asyncio.gather(a.take_pending("late", now=later), b.take_pending("late", now=later))
    assert results == [None, None]


async def test_connects_racing_from_two_pools_never_share_a_version(stores):
    a, b = stores
    async with asyncio.timeout(_BOUND_S):
        await a.replace_tokens(_token_row(a))
        results = await asyncio.gather(*((a if n % 2 else b).replace_tokens(_token_row(a, access_token_enc=a.seal(f"tok-{n}"))) for n in range(8)))
        final = await b.get_tokens("kb")
    assert sorted(r.version for r in results) == list(range(2, 10))
    assert final.version == 9


async def test_only_one_pool_wins_the_refresh_lease_and_its_holder_can_release_it(stores):
    a, b = stores
    async with asyncio.timeout(_BOUND_S):
        row = await a.replace_tokens(_token_row(a))
        guard = {"version": [raw_generation(row, "version")], "refresh_lease_until": [raw_generation(row, "refresh_lease_until")]}
        until = NOW + timedelta(seconds=30, microseconds=250000)
        results = await asyncio.gather(*(
            (a if n % 2 else b).patch_tokens("kb", {"refresh_lease_until": until, "refresh_lease_owner": f"pod-{n}"}, where=guard) for n in range(8)
        ))
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        leased = await b.get_tokens("kb")
        assert leased.refresh_lease_owner == winners[0].refresh_lease_owner and leased.refresh_lease_until == until

        released = await a.patch_tokens(
            "kb", {"refresh_lease_until": None, "refresh_lease_owner": None},
            where={"refresh_lease_until": [raw_generation(leased, "refresh_lease_until")], "refresh_lease_owner": [leased.refresh_lease_owner]},
        )
    assert released is not None and released.refresh_lease_until is None


async def test_the_table_holds_ciphertext_only(stores, pools):
    a, _ = stores
    async with asyncio.timeout(_BOUND_S):
        await a.replace_tokens(_token_row(a))
        await a.put_client(McpOAuthClient(id="kb", issuer="https://as.example", redirect_uri="https://p.example/cb", client_id="c1",
                                          client_secret_enc=a.seal("client-secret-plain"), registered_at=NOW))
        await a.put_pending(_pending_row(a, "state-1"))
        async with pools[0].pool.acquire() as connection:
            schema = pools[0].schema
            documents = [
                str(await connection.fetchval(f'SELECT data::text FROM "{schema}"."{table}"'))
                for table in ("mcpoauthtokenset", "mcpoauthclient", "mcpoauthpendingsignin")
            ]
    assert all(documents), "a table was empty, so its check proved nothing"
    for document in documents:
        for plain in ("tok-plain", "ref-plain", "client-secret-plain", "verifier-plain", "state-1"):
            assert plain not in document
