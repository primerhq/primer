import pytest

from primer.model.web_fetch import (
    ACTIVE_WEB_FETCH_CONFIG_ID, ActiveWebFetchConfig, WebFetchProvider,
)


@pytest.mark.asyncio
async def test_bootstrap_seeds_local_and_active_config(app):
    sp = app.state.storage_provider
    assert await sp.get_storage(WebFetchProvider).get("local") is not None
    ac = await sp.get_storage(ActiveWebFetchConfig).get(ACTIVE_WEB_FETCH_CONFIG_ID)
    assert ac is not None
    assert ac.config.provider_id == "local"
