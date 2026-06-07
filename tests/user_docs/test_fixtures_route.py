"""Route test for GET /v1/user_docs/_fixtures/{name}.json.

Uses the async TestClient pattern from tests/api/ so no running server
is required.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport

from primer.api.app import create_test_app


@pytest_asyncio.fixture
async def client():
    """Minimal test client with the real app (no auth needed for fixtures)."""
    from tests.conftest import _FakeStorageProvider
    from primer.api.registries import ProviderRegistry

    sp = _FakeStorageProvider()
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        # Register + log in so auth-protected routes are reachable.
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:
            pass
        yield c


class TestFixturesRoute:
    @pytest.mark.asyncio
    async def test_agents_page_fixture_returns_200_with_weekly_digest(
        self, client: httpx.AsyncClient
    ) -> None:
        r = await client.get("/v1/user_docs/_fixtures/agents-page.json")
        assert r.status_code == 200, r.text
        body = r.json()
        # Must be valid JSON and contain the weekly-digest agent in the offset
        # page keyed by "GET /agents?limit=200&offset=0".
        key = "GET /agents?limit=200&offset=0"
        assert key in body, f"expected key {key!r} in fixture body; got keys {list(body.keys())}"
        items = body[key]["items"]
        ids = [item["id"] for item in items]
        assert "weekly-digest" in ids, f"expected 'weekly-digest' in fixture items; got {ids}"

    @pytest.mark.asyncio
    async def test_missing_fixture_returns_404(
        self, client: httpx.AsyncClient
    ) -> None:
        r = await client.get("/v1/user_docs/_fixtures/does-not-exist.json")
        assert r.status_code == 404, r.text

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        r = await client.get("/v1/user_docs/_fixtures/../../secret.json")
        assert r.status_code in (400, 404, 422), r.text
