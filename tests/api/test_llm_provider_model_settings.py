"""GET /v1/llm_providers/{provider_id}/model_settings: publish a provider's settings catalogue (PMS task 5).

The route returns the wire's catalogue (task 1) plus the provider type, its flavor and the universal
reasoning levels; the ``@route_tool`` twin answers the same object through the application, so the
console's "Provider settings" form and an agent writing a profile read the identical surface.
"""

from __future__ import annotations

import json

import pytest
from pydantic import HttpUrl, SecretStr

from primer.api import route_tools
from primer.model.common import dump_for_storage
from primer.model.principal import PrincipalRef
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)
from primer.model.yield_ import ToolContext


def _vllm(provider_id: str) -> LLMProvider:
    return LLMProvider(
        id=provider_id,
        provider=LLMProviderType.OPENCHAT,
        config=OpenChatConfig(
            url=HttpUrl("http://127.0.0.1:8000/v1/"),
            flavor=OpenChatFlavor.VLLM,
        ),
        limits=Limits(max_concurrency=4),
    )


def _anthropic(provider_id: str) -> LLMProvider:
    return LLMProvider(
        id=provider_id,
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key=SecretStr("sk-test")),
        limits=Limits(max_concurrency=4),
    )


async def _seed_provider(client, provider_id: str, provider: LLMProvider) -> None:
    r = await client.post("/v1/llm_providers", json=dump_for_storage(provider))
    assert r.status_code in (200, 201), r.text


class TestRestModelSettings:
    @pytest.mark.asyncio
    async def test_a_vllm_provider_returns_its_wire_catalogue(self, client) -> None:
        await _seed_provider(client, "ms-vllm", _vllm("ms-vllm"))
        r = await client.get("/v1/llm_providers/ms-vllm/model_settings")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider_type"] == "openchat"
        assert body["flavor"] == "vllm"
        assert body["wire"] == "openchat/vllm"
        assert body["body_passthrough"] is True
        top_k = next(s for s in body["settings"] if s["key"] == "top_k")
        assert top_k["type"] == "integer"
        assert body["custom_reasoning"]["key"] == "reasoning_effort"

    @pytest.mark.asyncio
    async def test_an_anthropic_provider_carries_its_closed_fields(self, client) -> None:
        await _seed_provider(client, "ms-anthropic", _anthropic("ms-anthropic"))
        r = await client.get("/v1/llm_providers/ms-anthropic/model_settings")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider_type"] == "anthropic"
        assert body["flavor"] is None
        assert body["wire"] == "anthropic"
        assert body["body_passthrough"] is False
        thinking = next(s for s in body["settings"] if s["key"] == "thinking")
        assert thinking["fields"], "thinking is a closed object with named fields"
        assert body["custom_reasoning"]["key"] == "output_config.effort"

    @pytest.mark.asyncio
    async def test_an_unknown_provider_is_404(self, client) -> None:
        r = await client.get("/v1/llm_providers/no-such-provider/model_settings")
        assert r.status_code == 404, r.text

    @pytest.mark.asyncio
    async def test_reasoning_levels_end_with_xhigh_and_max(self, client) -> None:
        await _seed_provider(client, "ms-rl", _anthropic("ms-rl"))
        r = await client.get("/v1/llm_providers/ms-rl/model_settings")
        body = r.json()
        assert body["reasoning_levels"][-2:] == ["xhigh", "max"]


class TestRouteTool:
    @pytest.mark.asyncio
    async def test_the_tool_is_listed_and_returns_the_same_object(self, app, client) -> None:
        await _seed_provider(client, "ms-tool", _vllm("ms-tool"))
        rest = (await client.get("/v1/llm_providers/ms-tool/model_settings")).json()

        snapshot = route_tools.build_generated_route_toolsets(app)
        provider = snapshot.providers["llm_providers"]
        names = [tool.id async for tool in provider.list_tools()]
        assert "get_llm_provider_model_settings" in names

        ctx = ToolContext(
            tool_call_id="call", session_id="s-1", workspace_id="ws-1",
            turn_no=1, initiated_by=PrincipalRef.system(),
        )
        result = await provider.call(
            tool_name="get_llm_provider_model_settings",
            arguments={"provider_id": "ms-tool"},
            ctx=ctx,
        )
        assert not result.is_error
        assert json.loads(result.output) == rest
