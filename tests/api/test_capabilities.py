"""GET /v1/capabilities reports installed optional extras."""

from __future__ import annotations

import pytest

import primer.common.optional as optional_mod
from primer.api.version import APP_VERSION
from primer.common.optional import EXTRA_MODULES


@pytest.mark.asyncio
async def test_capabilities_shape(client) -> None:
    response = await client.get("/v1/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == APP_VERSION
    assert set(body["extras"]) == set(EXTRA_MODULES)
    for extra, status in body["extras"].items():
        assert isinstance(status["installed"], bool)
        if extra == "channels":
            assert set(status["platforms"]) == {"slack", "telegram", "discord"}
        else:
            assert status["platforms"] is None


@pytest.mark.asyncio
async def test_capabilities_reflects_missing_extras(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(optional_mod, "_find_spec", lambda name: None)
    response = await client.get("/v1/capabilities")
    body = response.json()
    assert all(not s["installed"] for s in body["extras"].values())
    assert body["extras"]["channels"]["platforms"] == {
        "slack": False,
        "telegram": False,
        "discord": False,
    }
