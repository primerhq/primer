"""Two server processes refresh one rotating refresh token at once, on a live Postgres (plan 07, Task 8; master Review Focus 3; plan Review Focus 1).

Each process is a ``ToolsetOAuthService`` over its own ``PostgresStorageProvider`` (its own connection pool) on one throwaway schema, with its own HTTP
client and lease owner. The token endpoint is an ``httpx.MockTransport`` handler that rotates refresh tokens (each works once: a reused one answers
``400 invalid_grant``), counts the exchanges and answers slowly, so the callers of both processes overlap for real. The in-memory fake of
``tests/toolset/oauth/test_service_refresh.py`` never interleaves two awaits on the database; only this file shows the lease holds when the callers are
different connections.

Expected, round after round: ONE exchange, every caller gets the new access token, the stored refresh token is the rotated one, the toolset stays
connected and the lease is released.

Gate: ``PRIMER_TEST_POSTGRES_URL`` (see ``tests/pg_gate.py``); the schema is a throwaway one, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio

from primer.common.secret_box import SecretBox
from primer.model.mcp_oauth import McpOAuthTokenSet
from primer.model.provider import PoolConfig, PostgresConfig, Toolset
from primer.storage import postgres as pg_backend
from primer.storage import raw_generation
from primer.storage.postgres import PostgresStorageProvider
from primer.toolset.oauth.service import OAuthTarget, ToolsetOAuthService, config_fingerprint
from primer.toolset.oauth.store import McpOAuthStore
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_WHAT = "the MCP OAuth cross-process refresh race test"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]

KEY = "live-refresh-key-0123456789"
TOKEN_ENDPOINT = "https://as.example/token"
#: Every wait is bounded: a refresh that never ends fails the test instead of hanging the lane.
_BOUND_S = 60.0


class RotatingTokenEndpoint:
    """A token endpoint whose refresh tokens work once. A refresh token is spent when its request ARRIVES, as a real server does, so a second
    exchange of the same token, however close behind, is refused; the answer comes after ``delay_s``."""

    def __init__(self, first_refresh: str, *, delay_s: float = 0.3) -> None:
        self.live = {first_refresh}
        self.exchanges = 0
        self.refused = 0
        self.delay_s = delay_s

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        form = {name: values[0] for name, values in parse_qs(request.content.decode("utf-8")).items()}
        assert form.get("grant_type") == "refresh_token", form.get("grant_type")
        self.exchanges += 1
        number = self.exchanges
        presented = form.get("refresh_token")
        valid = presented in self.live
        self.live.discard(presented)
        await asyncio.sleep(self.delay_s)
        if not valid:
            self.refused += 1
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "the refresh token was already used"})
        self.live.add(f"rt-{number}")
        return httpx.Response(200, json={"access_token": f"at-{number}", "refresh_token": f"rt-{number}", "token_type": "Bearer", "expires_in": 3600})


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
    schema = f"mcpoauthrefresh{uuid.uuid4().hex[:12]}"
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


def _target() -> OAuthTarget:
    toolset = Toolset(id="kb", provider="mcp", config={"transport": "http", "config": {"url": "https://m.example/mcp", "oauth": {}}})
    target = OAuthTarget.from_toolset(toolset)
    assert target is not None
    return target


async def test_two_postgres_pools_refresh_once(pools):
    endpoint = RotatingTokenEndpoint("rt-0")
    box = SecretBox.from_key_material(KEY)
    target = _target()
    store = McpOAuthStore(pools[0], box)
    now = datetime.now(UTC)
    clients = [httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) for _ in pools]
    services = [ToolsetOAuthService(storage_provider=pool, secret_box=box, http=client, owner_id=f"pod-{n}")
                for n, (pool, client) in enumerate(zip(pools, clients, strict=True))]
    try:
        async with asyncio.timeout(_BOUND_S):
            await store.replace_tokens(McpOAuthTokenSet(
                id="kb", grant="authorization_code", access_token_enc=store.seal("at-0"), refresh_token_enc=store.seal("rt-0"),
                expires_at=now + timedelta(seconds=10), token_endpoint=TOKEN_ENDPOINT, client_id="client-1", client_source="registered",
                config_fingerprint=config_fingerprint(target), connected_at=now,
            ))
            for round_number in (1, 2):
                tokens = await asyncio.gather(*(services[n % 2].access_token(target) for n in range(8)))
                assert set(tokens) == {f"at-{round_number}"}, "every caller of both processes got the one new access token"
                assert (endpoint.exchanges, endpoint.refused) == (round_number, 0), "one exchange per round, and no refresh token was spent twice"
                row = await store.get_tokens("kb")
                assert store.unseal(row.refresh_token_enc) == f"rt-{round_number}", "the stored refresh token is the rotated one"
                assert (row.state, row.version, row.refresh_lease_until, row.refresh_lease_owner) == ("connected", round_number + 1, None, None)
                # The next round starts from the rotated token, close to its expiry again.
                await store.patch_tokens("kb", {"expires_at": datetime.now(UTC) + timedelta(seconds=10)}, where={"version": [raw_generation(row, "version")]})
    finally:
        for service in services:
            await service.aclose()
        for client in clients:
            await client.aclose()
