from __future__ import annotations

import pytest

from primer.api.app import create_test_app
from primer.api.config import AppConfig
from primer.api.registries.provider_registry import _build_default_toolset_factory
from primer.model.provider import Toolset
from primer.toolset.mcp import McpToolsetProvider
from primer.toolset.oauth.service import ToolsetOAuthService
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401


def _toolset() -> Toolset:
    return Toolset.model_validate({
        "id": "kb",
        "provider": "mcp",
        "config": {"transport": "http", "config": {"url": "https://mcp.example/mcp", "oauth": {}}},
    })


def test_the_factory_hands_the_service_to_mcp_providers() -> None:
    service = object()
    provider = _build_default_toolset_factory(oauth=service)(_toolset())
    assert isinstance(provider, McpToolsetProvider)
    assert provider._oauth is service


def test_the_test_app_carries_the_service(fake_storage_provider, fake_provider_registry) -> None:
    app = create_test_app(storage_provider=fake_storage_provider, provider_registry=fake_provider_registry)
    assert isinstance(app.state.mcp_oauth, ToolsetOAuthService)


def test_public_url_is_normalised() -> None:
    assert AppConfig(public_url="https://primer.example/").public_url == "https://primer.example"
    with pytest.raises(ValueError, match="public_url must be an absolute"):
        AppConfig(public_url="primer.example")


@pytest.mark.asyncio
async def test_an_oauth_toolset_is_creatable_before_it_is_connected(client, mock_oauth_mcp) -> None:
    response = await client.post(
        "/v1/toolsets",
        json={
            "id": "oauth-before-connect",
            "provider": "mcp",
            "config": {"transport": "http", "config": {"url": mock_oauth_mcp.mcp_url, "oauth": {}}},
        },
    )

    assert response.status_code == 201, response.text
