"""Phase-2 router tests: Agent + Graph CRUD/Find/Status."""

from __future__ import annotations

from primer.model_profile import ResolvedModel
from primer.model.model_profile import ModelProfileConfig

import pytest
from pydantic import SecretStr

from primer.model.agent import Agent, AgentModel


def _agent(**overrides) -> Agent:
    body = dict(
        id="agt-1",
        description="test agent",
        model=AgentModel(profile_id="anthropic-1--claude-sonnet-4-6"),
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
        """The platform seeds an operator and a builder agent, so a list is
        never empty; assert this agent joined it rather than a count."""
        before = (await client.get("/v1/agents?limit=50&offset=0")).json()["length"]
        body = _agent().model_dump(mode="json")
        await client.post("/v1/agents", json=body)
        listed = await client.get("/v1/agents?limit=50&offset=0")
        assert listed.status_code == 200
        assert listed.json()["length"] == before + 1
        assert "agt-1" in [row["id"] for row in listed.json()["items"]]


class TestAgentStatus:
    @pytest.mark.asyncio
    async def test_status_ok_when_provider_exists(
        self, client, fake_storage_provider
    ) -> None:
        from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
)

        await fake_storage_provider.get_storage(LLMProvider).create(
            LLMProvider(
                id="anthropic-1",
                provider=LLMProviderType.ANTHROPIC,
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
    AnthropicConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
)
        await fake_storage_provider.get_storage(LLMProvider).create(
            LLMProvider(
                id="anthropic-1",
                provider=LLMProviderType.ANTHROPIC,
                config=AnthropicConfig(api_key=SecretStr("x")),
                limits=Limits(max_concurrency=4),
            )
        )
        body = _agent(tools=[
            "web__http_request",
            "web__web_search",
            "system__list_files",
            "workspaces__create_workspace",
            "workspace_ext__sleep",
            "harness__list",
        ]).model_dump(mode="json")
        await client.post("/v1/agents", json=body)
        resp = await client.get("/v1/agents/agt-1/status")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "issues": []}


class TestAgentSearch:
    """`GET /v1/agents?q=` case-insensitive substring search over the
    entity's search_fields (id + description), same {items,total} shape."""

    async def _seed(self, client) -> None:
        for aid, desc in (
            ("agt-alpha", "First HELPER agent"),
            ("agt-beta", "second helper bot"),
            ("agt-gamma", "unrelated widget"),
        ):
            body = _agent(id=aid, description=desc).model_dump(mode="json")
            resp = await client.post("/v1/agents", json=body)
            assert resp.status_code == 201, resp.text

    @pytest.mark.asyncio
    async def test_q_filters_case_insensitively(self, client) -> None:
        await self._seed(client)
        resp = await client.get("/v1/agents", params={"q": "helper"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Identical offset-paged envelope to the unfiltered list.
        assert {"items", "total", "offset", "length"} <= set(body)
        assert sorted(a["id"] for a in body["items"]) == ["agt-alpha", "agt-beta"]
        assert body["total"] == 2

    @pytest.mark.asyncio
    async def test_q_matches_on_id_field(self, client) -> None:
        await self._seed(client)
        # 'gamma' appears only in the id, and the query is upper-cased.
        resp = await client.get("/v1/agents", params={"q": "GAMMA"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [a["id"] for a in body["items"]] == ["agt-gamma"]
        assert body["total"] == 1

    @pytest.mark.asyncio
    async def test_q_absent_is_unfiltered(self, client) -> None:
        await self._seed(client)
        resp = await client.get("/v1/agents?limit=50&offset=0")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The three seeded here, plus the operator and builder the platform
        # seeds itself.
        ids = [row["id"] for row in body["items"]]
        assert {"agt-alpha", "agt-beta", "agt-gamma"} <= set(ids)
        assert body["length"] == body["total"]

    @pytest.mark.asyncio
    async def test_q_percent_is_literal(self, client) -> None:
        for aid, desc in (
            ("agt-pct", "50% discount"),
            ("agt-plain", "50 percent discount"),
        ):
            body = _agent(id=aid, description=desc).model_dump(mode="json")
            assert (await client.post("/v1/agents", json=body)).status_code == 201
        # A user typing '50%' searches for a LITERAL '50%', not "50 + wildcard".
        resp = await client.get("/v1/agents", params={"q": "50%"})
        assert resp.status_code == 200, resp.text
        assert [a["id"] for a in resp.json()["items"]] == ["agt-pct"]


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
