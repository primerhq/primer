"""End-to-end tests for the four Phase-1 provider routers.

Covers the standard CRUD + Find loop for one entity (LLMProvider) in
detail; the other three providers (EmbeddingProvider,
CrossEncoderProvider, Toolset) get smoke tests since they share the
same router factory.

Also covers entity-specific endpoints:
* ``GET    /v1/<provider>/{id}/models``  -- live model list
* ``POST   /v1/<provider>/{id}/invalidate``
* ``GET    /v1/toolsets/{id}/tools``     -- live tool list
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from primer.api.registries import ProviderRegistry
from primer.model.provider import (
    AnthropicConfig,
    CrossEncoderModel,
    CrossEncoderProvider,
    CrossEncoderProviderType,
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HttpConfig,
    HuggingFaceConfig,
    HuggingFaceCrossEncoderConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    McpConfig,
    OAuthConfig,
    StdioConfig,
    Toolset,
    ToolsetProviderType,
    TransportType,
)


def _llm() -> LLMProvider:
    return LLMProvider(
        id="anthropic-1",
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key=SecretStr("sk-x")),
        limits=Limits(max_concurrency=4),
    )


def _embedding() -> EmbeddingProvider:
    return EmbeddingProvider(
        id="hf-1",
        provider=EmbeddingProviderType.HUGGINGFACE,
        models=[EmbeddingModel(name="sentence-transformers/all-MiniLM-L6-v2")],
        config=HuggingFaceConfig(token=SecretStr("hf_x")),
        limits=Limits(max_concurrency=2),
    )


def _cross_encoder() -> CrossEncoderProvider:
    return CrossEncoderProvider(
        id="ce-1",
        provider=CrossEncoderProviderType.HUGGINGFACE,
        models=[CrossEncoderModel(name="BAAI/bge-reranker-v2-m3")],
        config=HuggingFaceCrossEncoderConfig(token=None),
        limits=Limits(max_concurrency=2),
    )


def _toolset() -> Toolset:
    return Toolset(
        id="ts-1",
        provider=ToolsetProviderType.MCP,
        config=McpConfig(
            transport=TransportType.STDIO,
            config=StdioConfig(command=["echo"]),
        ),
    )


# ===========================================================================
# CRUD + Find — exercised against LLMProvider; the same router factory
# powers the other three so they only get smoke checks below.
# ===========================================================================


class TestLLMProviderCRUD:
    @pytest.mark.asyncio
    async def test_create_then_get_round_trip(self, client) -> None:
        body = _llm().model_dump(mode="json")
        resp = await client.post("/v1/llm_providers", json=body)
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == "anthropic-1"

        get = await client.get("/v1/llm_providers/anthropic-1")
        assert get.status_code == 200
        assert get.json()["id"] == "anthropic-1"

    @pytest.mark.asyncio
    async def test_create_duplicate_returns_409_conflict(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        dup = await client.post("/v1/llm_providers", json=body)
        assert dup.status_code == 409
        assert dup.json()["type"] == "/errors/conflict"

    @pytest.mark.asyncio
    async def test_get_unknown_returns_404(self, client) -> None:
        resp = await client.get("/v1/llm_providers/missing")
        assert resp.status_code == 404
        assert resp.json()["type"] == "/errors/not-found"

    @pytest.mark.asyncio
    async def test_put_updates_when_path_id_matches(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        body["limits"]["max_concurrency"] = 8
        put = await client.put("/v1/llm_providers/anthropic-1", json=body)
        assert put.status_code == 200
        assert put.json()["limits"]["max_concurrency"] == 8

    @pytest.mark.asyncio
    async def test_put_with_mismatched_id_returns_409(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        put = await client.put("/v1/llm_providers/different-id", json=body)
        assert put.status_code == 409

    @pytest.mark.asyncio
    async def test_put_unknown_returns_404(self, client) -> None:
        body = _llm().model_dump(mode="json")
        put = await client.put("/v1/llm_providers/anthropic-1", json=body)
        assert put.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_then_get_returns_404(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        delete = await client.delete("/v1/llm_providers/anthropic-1")
        assert delete.status_code == 204
        get = await client.get("/v1/llm_providers/anthropic-1")
        assert get.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_unknown_returns_404(self, client) -> None:
        resp = await client.delete("/v1/llm_providers/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_paginates(self, client) -> None:
        for i in range(3):
            body = _llm().model_dump(mode="json")
            body["id"] = f"row-{i}"
            await client.post("/v1/llm_providers", json=body)
        listed = await client.get("/v1/llm_providers?limit=2&offset=0")
        assert listed.status_code == 200
        page = listed.json()
        assert page["kind"] == "offset"
        assert page["length"] == 2
        assert page["total"] == 3

    @pytest.mark.asyncio
    async def test_find_returns_offset_page(self, client) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        find = await client.post(
            "/v1/llm_providers/find",
            json={"page": {"kind": "offset", "offset": 0, "length": 20}},
        )
        assert find.status_code == 200
        assert find.json()["length"] == 1


# ===========================================================================
# Cascade invalidation — PUT/DELETE drop the cached adapter.
# ===========================================================================


class TestCascadeInvalidation:
    @pytest.mark.asyncio
    async def test_put_invalidates_cached_llm(
        self, client, fake_provider_registry
    ) -> None:
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        registry: ProviderRegistry = fake_provider_registry

        sentinel_v1 = MagicMock()
        sentinel_v1.aclose = AsyncMock()
        sentinel_v2 = MagicMock()
        sentinel_v2.aclose = AsyncMock()
        registry._llm_factory = lambda _p: sentinel_v1  # type: ignore[attr-defined]
        first = await registry.get_llm("anthropic-1")
        assert first is sentinel_v1

        registry._llm_factory = lambda _p: sentinel_v2  # type: ignore[attr-defined]
        body["limits"]["max_concurrency"] = 8
        put = await client.put("/v1/llm_providers/anthropic-1", json=body)
        assert put.status_code == 200

        second = await registry.get_llm("anthropic-1")
        assert second is sentinel_v2
        sentinel_v1.aclose.assert_awaited_once()


# ===========================================================================
# /models live-fetch endpoint
# ===========================================================================


class TestLiveModelsEndpoint:
    @pytest.mark.asyncio
    async def test_returns_model_names_from_profiles(self, client) -> None:
        """The endpoint reads profiles, not the adapter.

        Profiles replaced the models[] allowlist as the registry of what a
        provider serves, so this is a storage query. The live upstream
        probe is _discover_models.
        """
        body = _llm().model_dump(mode="json")
        await client.post("/v1/llm_providers", json=body)
        for pid, model in [
            ("anthropic-1--sonnet", "claude-sonnet-4-6"),
            ("anthropic-1--haiku", "haiku-4"),
        ]:
            r = await client.post("/v1/model_profiles", json={
                "id": pid, "description": f"{model} profile.",
                "provider_id": "anthropic-1", "model_name": model,
                "context_length": 200000,
            })
            assert r.status_code in (200, 201), r.text

        resp = await client.get("/v1/llm_providers/anthropic-1/models")
        assert resp.status_code == 200
        assert resp.json() == {"models": ["claude-sonnet-4-6", "haiku-4"]}

    @pytest.mark.asyncio
    async def test_two_profiles_on_one_model_dedupe(self, client) -> None:
        """Two profiles may share a model name; the list is distinct names."""
        await client.post("/v1/llm_providers", json=_llm().model_dump(mode="json"))
        for pid, reasoning in [("a-fast", "off"), ("a-think", "high")]:
            r = await client.post("/v1/model_profiles", json={
                "id": pid, "description": f"sonnet {reasoning}.",
                "provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6",
                "context_length": 200000, "config": {"reasoning": reasoning},
            })
            assert r.status_code in (200, 201), r.text

        resp = await client.get("/v1/llm_providers/anthropic-1/models")
        assert resp.json() == {"models": ["claude-sonnet-4-6"]}

    @pytest.mark.asyncio
    async def test_empty_when_provider_has_no_profiles(self, client) -> None:
        """Distinct from a missing provider, which 404s.

        Uses its own provider id: the in-memory storage is shared across
        this class's tests, so reusing anthropic-1 would see the profiles
        the sibling tests created.
        """
        body = _llm().model_dump(mode="json")
        body["id"] = "anthropic-no-profiles"
        await client.post("/v1/llm_providers", json=body)
        resp = await client.get("/v1/llm_providers/anthropic-no-profiles/models")
        assert resp.status_code == 200
        assert resp.json() == {"models": []}

    @pytest.mark.asyncio
    async def test_404_when_provider_missing(self, client) -> None:
        resp = await client.get("/v1/llm_providers/missing/models")
        assert resp.status_code == 404


# ===========================================================================
# Smoke tests for the other three provider routers (CRUD + invalidate).
# ===========================================================================


class TestEmbeddingProviderSmoke:
    @pytest.mark.asyncio
    async def test_crud_round_trip(self, client) -> None:
        body = _embedding().model_dump(mode="json")
        assert (await client.post("/v1/embedding_providers", json=body)).status_code == 201
        assert (await client.get("/v1/embedding_providers/hf-1")).status_code == 200
        assert (
            await client.delete("/v1/embedding_providers/hf-1")
        ).status_code == 204

    @pytest.mark.asyncio
    async def test_explicit_invalidate(self, client) -> None:
        body = _embedding().model_dump(mode="json")
        await client.post("/v1/embedding_providers", json=body)
        resp = await client.post("/v1/embedding_providers/hf-1/invalidate")
        assert resp.status_code == 204


class TestCrossEncoderProviderSmoke:
    @pytest.mark.asyncio
    async def test_crud_round_trip(self, client) -> None:
        body = _cross_encoder().model_dump(mode="json")
        assert (
            await client.post("/v1/cross_encoder_providers", json=body)
        ).status_code == 201
        assert (
            await client.get("/v1/cross_encoder_providers/ce-1")
        ).status_code == 200
        assert (
            await client.delete("/v1/cross_encoder_providers/ce-1")
        ).status_code == 204


class TestToolsetSmoke:
    @pytest.mark.asyncio
    async def test_crud_round_trip(self, client) -> None:
        body = _toolset().model_dump(mode="json")
        assert (await client.post("/v1/toolsets", json=body)).status_code == 201
        assert (await client.get("/v1/toolsets/ts-1")).status_code == 200
        assert (await client.delete("/v1/toolsets/ts-1")).status_code == 204

    @pytest.mark.asyncio
    async def test_list_tools_returns_tool_descriptors(
        self, client, fake_provider_registry
    ) -> None:
        body = _toolset().model_dump(mode="json")
        await client.post("/v1/toolsets", json=body)

        async def _gen(*, principal=None):
            for tn in ("foo", "bar"):
                tool = MagicMock()
                tool.model_dump = MagicMock(return_value={"id": tn})
                # Real flag values, not MagicMock auto-attributes (a bare
                # mock's getattr result is truthy) -- this pins the y/w/r/n
                # badge fields the route now adds back explicitly.
                tool.yields = tn == "foo"
                tool.requires_workspace = tn == "bar"
                tool.tool_class = "standard"
                tool.required_role = None
                yield tool

        provider_mock = MagicMock()
        provider_mock.list_tools = _gen
        provider_mock.aclose = AsyncMock()
        fake_provider_registry._toolset_factory = lambda _t: provider_mock  # type: ignore[attr-defined]

        resp = await client.get("/v1/toolsets/ts-1/tools")
        assert resp.status_code == 200
        assert resp.json() == {
            "tools": [
                {
                    "id": "foo", "yields": True, "requires_workspace": False,
                    "tool_class": "standard", "required_role": None,
                },
                {
                    "id": "bar", "yields": False, "requires_workspace": True,
                    "tool_class": "standard", "required_role": None,
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_list_tools_badges_reflect_notifying_and_role_gating(
        self, client, fake_provider_registry
    ) -> None:
        # A second pass so "standard"/None aren't the only values ever
        # exercised -- notifying + role-gated are real, distinct states.
        body = Toolset(
            id="ts-2", provider=ToolsetProviderType.MCP,
            config=McpConfig(
                transport=TransportType.STDIO,
                config=StdioConfig(command=["echo"]),
            ),
        ).model_dump(mode="json")
        await client.post("/v1/toolsets", json=body)

        async def _gen(*, principal=None):
            tool = MagicMock()
            tool.model_dump = MagicMock(return_value={"id": "notify_admin"})
            tool.yields = False
            tool.requires_workspace = False
            tool.tool_class = "notifying"
            tool.required_role = "admin"
            yield tool

        provider_mock = MagicMock()
        provider_mock.list_tools = _gen
        provider_mock.aclose = AsyncMock()
        fake_provider_registry._toolset_factory = lambda _t: provider_mock  # type: ignore[attr-defined]

        resp = await client.get("/v1/toolsets/ts-2/tools")
        assert resp.status_code == 200
        tool = resp.json()["tools"][0]
        assert tool["tool_class"] == "notifying"
        assert tool["required_role"] == "admin"


class TestMcpOAuthCallback:
    """GET/POST /v1/toolsets/{id}/oauth/callback -- backend-gap-map #5.

    ``McpToolsetProvider.complete_oauth`` already existed; only the HTTP
    route that reaches it was missing (an operator's browser has nowhere
    to land after consenting to an MCP server's OAuth flow).
    """

    def _mcp_config(self, tid: str) -> "McpConfig":
        return McpConfig(
            transport=TransportType.HTTP,
            config=HttpConfig(
                url="http://mcp.example.invalid/",
                oauth=OAuthConfig(
                    redirect_uri=(
                        f"https://primer.example/v1/toolsets/{tid}/oauth/callback"
                    ),
                ),
            ),
        )

    async def _seed_mcp_toolset(self, client, tid: str) -> None:
        row = Toolset(id=tid, provider=ToolsetProviderType.MCP, config=self._mcp_config(tid))
        # The create-time reachability probe would otherwise try to
        # actually connect to the (fake, unresolvable) MCP url.
        resp = await client.post(
            "/v1/toolsets?allow_unreachable=true",
            json=row.model_dump(mode="json"),
        )
        assert resp.status_code == 201, resp.text

    def _seed_and_inject_real_provider(
        self, fake_provider_registry, tid: str,
    ) -> "McpToolsetProvider":
        """The shared ``client``/``app`` fixtures wire a dummy
        ``toolset_factory=lambda p: object()`` (see ``fake_provider_registry``
        in conftest.py), so ``registry.get_toolset`` never returns a real
        ``McpToolsetProvider`` by default -- build one directly and inject it,
        the same seam ``test_list_tools_returns_tool_descriptors`` above
        uses for its ``MagicMock``. A real instance is required here (not a
        MagicMock) so the route's ``isinstance(provider, McpToolsetProvider)``
        guard passes.

        Also builds a real ``PrimerOAuthHandler`` and passes it as
        ``oauth=`` -- the PRODUCTION default toolset factory
        (``_build_default_toolset_factory`` in provider_registry.py) never
        does this today (it only forwards ``config``/``allowed_stdio_commands``
        to ``McpToolsetProvider``, matching the class's own docstring: "In
        this sub-project ``oauth`` MUST be None ... sub-project #10 can
        wire OAuth in"). That means ``complete_oauth`` always 503s
        ("OAuth not configured") in the current build regardless of this
        route -- a separate, larger gap than "add the route" (flagged to
        the lead separately). Constructing the handler explicitly here
        tests THIS route's dispatch logic in isolation from that gap.
        """
        from primer.toolset.mcp import McpToolsetProvider
        from primer.toolset.oauth.handler import PrimerOAuthHandler

        cfg = self._mcp_config(tid)
        handler = PrimerOAuthHandler(
            oauth_config=cfg.config.oauth,
            mcp_url=cfg.config.url,
            toolset_id=tid,
        )
        provider = McpToolsetProvider(
            toolset_id=tid,
            config=cfg,
            oauth=handler,
            allowed_stdio_commands=None,
        )
        fake_provider_registry._toolset_factory = lambda _t: provider  # type: ignore[attr-defined]
        return provider

    @pytest.mark.asyncio
    async def test_valid_callback_reaches_the_provider(
        self, client, fake_provider_registry,
    ) -> None:
        await self._seed_mcp_toolset(client, "oauth-1")
        provider = self._seed_and_inject_real_provider(
            fake_provider_registry, "oauth-1",
        )
        provider.complete_oauth = AsyncMock()

        resp = await client.get(
            "/v1/toolsets/oauth-1/oauth/callback",
            params={"code": "auth-code-123", "state": "state-abc"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        provider.complete_oauth.assert_awaited_once_with(
            code="auth-code-123", state="state-abc",
        )

    @pytest.mark.asyncio
    async def test_post_variant_also_reaches_the_provider(
        self, client, fake_provider_registry,
    ) -> None:
        await self._seed_mcp_toolset(client, "oauth-2")
        provider = self._seed_and_inject_real_provider(
            fake_provider_registry, "oauth-2",
        )
        provider.complete_oauth = AsyncMock()

        resp = await client.post(
            "/v1/toolsets/oauth-2/oauth/callback",
            params={"code": "auth-code-456", "state": "state-def"},
        )
        assert resp.status_code == 200, resp.text
        provider.complete_oauth.assert_awaited_once_with(
            code="auth-code-456", state="state-def",
        )

    @pytest.mark.asyncio
    async def test_invalid_state_is_4xx(self, client, fake_provider_registry) -> None:
        """No mocking of ``complete_oauth`` itself: an unminted state
        genuinely fails at the OAuth handler's state-store lookup, which
        runs BEFORE any network call -- this exercises the real
        ``complete_oauth`` path end to end (only the provider construction
        seam is faked, same reason as the two tests above)."""
        await self._seed_mcp_toolset(client, "oauth-3")
        self._seed_and_inject_real_provider(fake_provider_registry, "oauth-3")
        resp = await client.get(
            "/v1/toolsets/oauth-3/oauth/callback",
            params={"code": "whatever", "state": "never-issued-state"},
        )
        assert 400 <= resp.status_code < 500, resp.text

    @pytest.mark.asyncio
    async def test_non_mcp_toolset_is_4xx(self, client) -> None:
        """``crud`` is a reserved, always-on internal (non-MCP) toolset."""
        resp = await client.get(
            "/v1/toolsets/crud/oauth/callback",
            params={"code": "x", "state": "y"},
        )
        assert 400 <= resp.status_code < 500, resp.text

    @pytest.mark.asyncio
    async def test_missing_toolset_is_404(self, client) -> None:
        resp = await client.get(
            "/v1/toolsets/does-not-exist/oauth/callback",
            params={"code": "x", "state": "y"},
        )
        assert resp.status_code == 404, resp.text


# ===========================================================================
# Live-probe URL stringification
# ===========================================================================


class TestProbeUrlStringification:
    """``config["url"]`` is typed ``HttpUrl`` on OllamaConfig / the shared
    ``_HttpApiKeyConfig`` (OpenResponsesConfig, OpenChatConfig) -
    ``model_dump()`` (mode="python", the default) hands back the HttpUrl
    OBJECT itself, not a string. Reproduced directly:
    ``OllamaConfig(url=...).model_dump()["url"]`` is a
    ``pydantic.networks.HttpUrl``, and passing it straight into
    ``ollama.AsyncClient(host=...)`` raises ``AttributeError: 'HttpUrl'
    object has no attribute 'partition'`` (the ollama SDK calls str-only
    methods on ``host`` internally); httpx's URL handling has the same
    failure mode on `f"{url}/models"` building via an un-stringified
    join. Both probes must stringify the url defensively regardless of
    whether the caller passes a raw request-body dict (already a plain
    str, JSON has no URL type) or a dict derived from a validated config
    model's dump (an HttpUrl).
    """

    @pytest.mark.asyncio
    async def test_probe_ollama_models_stringifies_the_url(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from primer.api.routers.providers import _probe_ollama_models
        from primer.model.providers.llm import OllamaConfig

        config = OllamaConfig(url="http://localhost:11434").model_dump()
        assert not isinstance(config["url"], str), (
            "sanity check: model_dump() must hand back a real HttpUrl "
            "object here, or this test is not exercising the bug"
        )

        captured: dict = {}

        class FakeAsyncClient:
            def __init__(self, *, host, headers=None):
                captured["host"] = host

            async def list(self):
                return {"models": []}

        monkeypatch.setattr("ollama.AsyncClient", FakeAsyncClient)
        result = await _probe_ollama_models(config)
        assert result == {"models": []}
        assert isinstance(captured["host"], str)
        assert captured["host"] == "http://localhost:11434/"

    @pytest.mark.asyncio
    async def test_probe_openai_compatible_models_stringifies_the_url(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from primer.api.routers.providers import _probe_openai_compatible_models
        from primer.model.providers.llm import OpenChatConfig

        config = OpenChatConfig(url="http://localhost:8080/v1").model_dump()
        assert not isinstance(config["url"], str), (
            "sanity check: model_dump() must hand back a real HttpUrl "
            "object here, or this test is not exercising the bug"
        )

        captured: dict = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"data": [{"id": "probe-model"}]}

        class FakeHttpxClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                captured["url"] = url
                return FakeResponse()

        monkeypatch.setattr(
            "httpx.AsyncClient",
            lambda *args, **kwargs: FakeHttpxClient(),
        )
        result = await _probe_openai_compatible_models(config)
        assert result == {"models": [{"name": "probe-model"}]}
        assert isinstance(captured["url"], str)
        assert captured["url"] == "http://localhost:8080/v1/models"
