"""S5 P2: the single boot probe carries the setup fact."""
from __future__ import annotations

from primer.bootstrap.setup_state import (
    MISSING_LLM_PROVIDER,
    MISSING_MODEL_PROFILE,
)
from primer.model.model_profile import ModelProfile


async def test_status_reports_incomplete_setup_before_the_wizard(client, app):
    app.state.storage_provider.get_storage(ModelProfile)._data.clear()
    r = await client.get("/v1/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["setup_complete"] is False
    assert MISSING_LLM_PROVIDER in body["setup_missing"]
    assert MISSING_MODEL_PROFILE in body["setup_missing"]


async def test_status_stays_public(raw_client, app):
    """`raw_client` carries no session cookie: the probe must still answer."""
    app.state.storage_provider.get_storage(ModelProfile)._data.clear()
    r = await raw_client.get("/v1/auth/status")
    assert r.status_code == 200
    assert r.json()["setup_complete"] is False
