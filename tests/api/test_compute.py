"""Phase-2 router tests: Agent + Graph CRUD/Find/Status."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.model.agent import Agent, AgentModel


def _agent(**overrides) -> Agent:
    body = dict(
        id="agt-1",
        description="test agent",
        model=AgentModel(provider_id="anthropic-1", model_name="claude-sonnet-4-6"),
        temperature=0.0,
        tools=[],
        system_prompt=["you are a test"],
    )
    body.update(overrides)
    return Agent(**body)


class TestAgentCRUD:
    @pytest.mark.asyncio
    async def test_round_trip(self, client) -> None:
        body = _agent().model_dump(mode="json")
        post = await client.post("/v1/agents", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/agents/agt-1")
        assert get.status_code == 200
        assert get.json()["id"] == "agt-1"

    @pytest.mark.asyncio
    async def test_list(self, client) -> None:
        body = _agent().model_dump(mode="json")
        await client.post("/v1/agents", json=body)
        listed = await client.get("/v1/agents?limit=20&offset=0")
        assert listed.status_code == 200
        assert listed.json()["length"] == 1


class TestAgentStatus:
    @pytest.mark.asyncio
    async def test_status_ok_when_provider_exists(
        self, client, fake_storage_provider
    ) -> None:
        from primer.model.provider import (
            AnthropicConfig,
            Limits,
            LLMModel,
            LLMProvider,
            LLMProviderType,
        )

        await fake_storage_provider.get_storage(LLMProvider).create(
            LLMProvider(
                id="anthropic-1",
                provider=LLMProviderType.ANTHROPIC,
                models=[LLMModel(name="claude-sonnet-4-6", context_length=200_000)],
                config=AnthropicConfig(api_key=SecretStr("x")),
                limits=Limits(max_concurrency=4),
            )
        )
        body = _agent().model_dump(mode="json")
        await client.post("/v1/agents", json=body)

        resp = await client.get("/v1/agents/agt-1/status")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "issues": []}

    @pytest.mark.asyncio
    async def test_status_flags_missing_provider(self, client) -> None:
        body = _agent().model_dump(mode="json")
        await client.post("/v1/agents", json=body)
        resp = await client.get("/v1/agents/agt-1/status")
        assert resp.status_code == 200
        result = resp.json()
        assert result["ok"] is False
        assert any("LLMProvider" in i for i in result["issues"])

    @pytest.mark.asyncio
    async def test_status_404_when_agent_missing(self, client) -> None:
        resp = await client.get("/v1/agents/missing/status")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_status_ignores_builtin_toolsets(
        self, client, fake_storage_provider,
    ) -> None:
        """Built-in toolsets (web/search/system/workspaces/misc/harness)
        have no Toolset storage row — the live registry resolves them
        directly. The status check must NOT flag them as missing."""
        from primer.model.provider import (
            AnthropicConfig, Limits, LLMModel, LLMProvider, LLMProviderType,
        )
        await fake_storage_provider.get_storage(LLMProvider).create(
            LLMProvider(
                id="anthropic-1",
                provider=LLMProviderType.ANTHROPIC,
                models=[LLMModel(name="claude-sonnet-4-6", context_length=200_000)],
                config=AnthropicConfig(api_key=SecretStr("x")),
                limits=Limits(max_concurrency=4),
            )
        )
        body = _agent(tools=[
            "web__http_request",
            "web__web_search",
            "search__semantic_search",
            "system__list_files",
            "workspaces__create_workspace",
            "workspace_ext__sleep",
            "harness__list",
        ]).model_dump(mode="json")
        await client.post("/v1/agents", json=body)
        resp = await client.get("/v1/agents/agt-1/status")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "issues": []}


class TestGraphCRUD:
    """Graph routes are smoke-tested only because constructing a valid
    Graph requires a fully populated topology of nodes/edges. The CRUD
    layer is identical to Agent's, which is fully exercised above."""

    @pytest.mark.asyncio
    async def test_404_on_unknown(self, client) -> None:
        resp = await client.get("/v1/graphs/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_status_404_on_unknown(self, client) -> None:
        resp = await client.get("/v1/graphs/missing/status")
        assert resp.status_code == 404
