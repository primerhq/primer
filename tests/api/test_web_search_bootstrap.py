"""Bootstrap tests — DDG row + active config singleton are created
once and re-runs are no-ops."""

from __future__ import annotations

import pytest

from primer.model.web_search import (
    ACTIVE_WEB_SEARCH_CONFIG_ID,
    ActiveWebSearchConfig,
    DuckDuckGoConfig,
    SingleProviderConfig,
    WebSearchProvider,
    WebSearchProviderType,
)


class TestBootstrap:
    @pytest.mark.asyncio
    async def test_first_run_creates_ddg_row(self, app) -> None:
        # Test app has bootstrap auto-run during fixture setup.
        sp = app.state.storage_provider
        row = await sp.get_storage(WebSearchProvider).get("DuckDuckGo")
        assert row is not None
        assert row.provider_type == WebSearchProviderType.DUCKDUCKGO
        assert isinstance(row.config, DuckDuckGoConfig)

    @pytest.mark.asyncio
    async def test_first_run_creates_active_config_pointing_at_ddg(
        self, app
    ) -> None:
        sp = app.state.storage_provider
        row = await sp.get_storage(ActiveWebSearchConfig).get(
            ACTIVE_WEB_SEARCH_CONFIG_ID,
        )
        assert row is not None
        assert isinstance(row.config, SingleProviderConfig)
        assert row.config.provider_id == "DuckDuckGo"

    @pytest.mark.asyncio
    async def test_second_run_is_idempotent(self, app) -> None:
        from primer.api.app import _bootstrap_web_search

        sp = app.state.storage_provider
        await _bootstrap_web_search(sp)  # second run
        # Rows still exist; no error.
        row = await sp.get_storage(WebSearchProvider).get("DuckDuckGo")
        assert row is not None
        row2 = await sp.get_storage(ActiveWebSearchConfig).get(
            ACTIVE_WEB_SEARCH_CONFIG_ID,
        )
        assert row2 is not None
