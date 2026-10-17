"""The web fetch active-config check and write, shared by the PUT route and the system tools (platform revamp 04, Task 1).

``primer/web_fetch/active_config.py`` holds the one "every id exists" check, its words and the singleton write. The REST PUT route
(``primer/api/routers/web_fetch.py``) and the ``set_web_fetch_active_config`` tool both call them, so the two cannot drift. Everything
here runs on a real sqlite store, seeded the way the lifespan seeds it (the ``local`` row, then the singleton naming it).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from primer.api._app_bootstrap import _bootstrap_web_fetch
from primer.api.errors import register_error_handlers
from primer.api.routers.web_fetch import web_fetch_active_config_router
from primer.model.provider import SqliteConfig
from primer.model.web_fetch import (
    ACTIVE_WEB_FETCH_CONFIG_ID,
    ActiveWebFetchConfig,
    AggregatedFetchConfig,
    SingleFetchConfig,
    WebFetchProvider,
)
from primer.storage.sqlite import SqliteStorageProvider
from primer.web_fetch.active_config import save_active_config, unknown_ids_message, unknown_provider_ids


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    await _bootstrap_web_fetch(provider)
    yield provider
    await provider.aclose()


async def test_unknown_provider_ids_lists_the_missing_ids_in_config_order(sp) -> None:
    cfg = AggregatedFetchConfig(provider_ids=["nope", "local", "gone"])

    assert await unknown_provider_ids(cfg, sp.get_storage(WebFetchProvider)) == ["nope", "gone"]


async def test_a_single_config_has_unknown_ids_only_when_its_row_is_missing(sp) -> None:
    providers = sp.get_storage(WebFetchProvider)

    assert await unknown_provider_ids(SingleFetchConfig(provider_id="local"), providers) == []
    assert await unknown_provider_ids(SingleFetchConfig(provider_id="nope"), providers) == ["nope"]


def test_the_message_is_the_rest_routes_words() -> None:
    assert unknown_ids_message(["nope"]) == "active config references unknown provider id(s): ['nope']"


async def test_save_creates_the_singleton_when_missing_then_replaces_it(sp) -> None:
    active = sp.get_storage(ActiveWebFetchConfig)
    await active.delete(ACTIVE_WEB_FETCH_CONFIG_ID)

    row = await save_active_config(SingleFetchConfig(provider_id="local"), active)
    assert row.id == ACTIVE_WEB_FETCH_CONFIG_ID

    await save_active_config(AggregatedFetchConfig(provider_ids=["local"]), active)
    stored = await active.get(ACTIVE_WEB_FETCH_CONFIG_ID)
    assert stored.config.mode == "aggregated" and stored.config.provider_ids == ["local"]


def test_the_put_route_uses_these_functions() -> None:
    from primer.api.routers import web_fetch as router

    assert router.unknown_provider_ids is unknown_provider_ids
    assert router.unknown_ids_message is unknown_ids_message
    assert router.save_active_config is save_active_config


class _CacheSpy:
    """Stands in for ``app.state.web_fetch_service``: counts the route's cache drops."""

    def __init__(self) -> None:
        self.dropped = 0

    def invalidate_active_config(self) -> None:
        self.dropped += 1


async def test_the_put_route_keeps_its_refusal_body_and_drops_the_cache_only_after_a_write(sp) -> None:
    """The refactor moved the route's check and write; its 422 body and its cache drop are what a console and the service rely on."""
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(web_fetch_active_config_router, prefix="/v1")
    app.state.storage_provider = sp
    app.state.web_fetch_service = spy = _CacheSpy()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.put(
            "/v1/web_fetch_active_config", json={"config": {"mode": "aggregated", "provider_ids": ["nope", "local", "gone"]}},
        )
        dropped_after_refusal = spy.dropped
        saved = await client.put("/v1/web_fetch_active_config", json={"config": {"mode": "aggregated", "provider_ids": ["local"]}})

    assert refused.status_code == 422, refused.text
    problem = refused.json()
    assert problem["detail"] == "active config references unknown provider id(s): ['nope', 'gone']"
    assert problem["extensions"] == {"error": "unknown_provider_ids", "message": problem["detail"], "unknown_ids": ["nope", "gone"]}
    assert dropped_after_refusal == 0, "a refused PUT dropped the service cache"
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"id": ACTIVE_WEB_FETCH_CONFIG_ID, "config": {"mode": "aggregated", "provider_ids": ["local"]}}
    assert spy.dropped == 1
    assert (await sp.get_storage(ActiveWebFetchConfig).get(ACTIVE_WEB_FETCH_CONFIG_ID)).config.provider_ids == ["local"]
