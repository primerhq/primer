"""Unit tests for matrix.api.app — create_app + lifespan + create_test_app."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr

from matrix.api.app import create_app, create_test_app
from matrix.api.config import AppConfig
from matrix.api.registries import ProviderRegistry, VectorStoreRegistry


def _config() -> AppConfig:
    return AppConfig(
        db_host="h",
        db_port=5432,
        db_database="d",
        db_user="u",
        db_password=SecretStr("p"),
    )


class TestCreateApp:
    def test_returns_fastapi_instance(self) -> None:
        app = create_app(_config())
        assert app.title == "Matrix Microagents Framework API"
        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/v1/health" in paths

    @pytest.mark.asyncio
    async def test_lifespan_seeds_app_state_and_aclose_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from matrix.api import app as app_mod

        sp_mock = MagicMock()
        sp_mock.initialize = AsyncMock()
        sp_mock.aclose = AsyncMock()
        monkeypatch.setattr(
            app_mod, "_build_storage_provider", lambda _config: sp_mock
        )

        app = create_app(_config())

        # ASGITransport does not drive lifespan events on its own; enter
        # the app's lifespan context manually so startup/shutdown run.
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/v1/health")
                assert response.status_code == 200
                assert app.state.storage_provider is sp_mock
                assert isinstance(app.state.provider_registry, ProviderRegistry)
                assert isinstance(app.state.vector_store_registry, VectorStoreRegistry)

        sp_mock.initialize.assert_awaited_once()
        sp_mock.aclose.assert_awaited_once()


class TestCreateTestApp:
    def test_seeds_state_directly(self) -> None:
        sp = MagicMock()
        pr = MagicMock(spec=ProviderRegistry)
        vsr = MagicMock(spec=VectorStoreRegistry)
        app = create_test_app(
            storage_provider=sp,
            provider_registry=pr,
            vector_store_registry=vsr,
        )
        assert app.state.storage_provider is sp
        assert app.state.provider_registry is pr
        assert app.state.vector_store_registry is vsr
