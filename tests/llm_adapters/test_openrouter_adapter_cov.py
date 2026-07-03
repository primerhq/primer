"""Coverage tests for the OpenRouterLLM adapter.

Placed outside ``tests/llm/`` so ``primer.llm.openrouter`` counts in the
CI unit sweep. The openai SDK transport is mocked with respx, so the
stream/discover paths run in-process with no network IO.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx
from pydantic import BaseModel as PydanticBaseModel
from pydantic import SecretStr

from primer.llm.openrouter import (
    OPENROUTER_BASE_URL,
    OpenRouterLLM,
    _attribution_headers,
    _discover_openrouter_models,
)
from primer.model.chat import (
    Done,
    Error as ChatError,
    Message,
    StreamStart,
    TextDelta,
    TextPart,
    Tool,
    Usage,
)
from primer.model.except_ import (
    BadRequestError,
    ConfigError,
    ModelNotFoundError,
    ProviderTimeoutError,
)
from primer.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenRouterConfig,
)


def _make_provider(
    *,
    api_key: str = "sk-or-v1-abc",
    app_name: str | None = None,
    app_url: str | None = None,
    models: list[str] | None = None,
    total_timeout_seconds: float | None = None,
) -> LLMProvider:
    return LLMProvider(
        id="or-cov",
        provider=LLMProviderType.OPENROUTER,
        config=OpenRouterConfig(
            api_key=SecretStr(api_key),
            app_name=app_name,
            app_url=app_url,
        ),
        models=[
            LLMModel(name=n, context_length=200000)
            for n in (models or ["anthropic/claude-3.5-sonnet"])
        ],
        limits=Limits(
            max_concurrency=4,
            total_timeout_seconds=total_timeout_seconds,
        ),
    )


_SSE = (
    b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
    b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
    b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
    b"data: [DONE]\n\n"
)


def _mock_stream() -> None:
    respx.post(f"{OPENROUTER_BASE_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_SSE, headers={"content-type": "text/event-stream"}
        )
    )


class TestAttributionHeaders:
    def test_both_fields(self) -> None:
        h = _attribution_headers(
            OpenRouterConfig(
                api_key=SecretStr("k"), app_name="primer", app_url="https://p.example"
            )
        )
        assert h["X-Title"] == "primer"
        assert h["HTTP-Referer"] == "https://p.example/"

    def test_only_name(self) -> None:
        h = _attribution_headers(OpenRouterConfig(api_key=SecretStr("k"), app_name="primer"))
        assert h == {"X-Title": "primer"}

    def test_neither(self) -> None:
        assert _attribution_headers(OpenRouterConfig(api_key=SecretStr("k"))) == {}


class TestConstructor:
    def test_valid(self) -> None:
        llm = OpenRouterLLM(_make_provider())
        assert llm._client is None

    def test_wrong_provider_type_raises(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", LLMProviderType.OPENCHAT)
        with pytest.raises(ConfigError, match="OPENROUTER"):
            OpenRouterLLM(provider)

    def test_wrong_config_type_raises(self) -> None:
        from pydantic import HttpUrl
        from primer.model.provider import OpenChatConfig, OpenChatFlavor

        provider = LLMProvider(
            id="x",
            provider=LLMProviderType.OPENROUTER,
            models=[LLMModel(name="anthropic/claude-3.5-sonnet", context_length=1000)],
            config=OpenChatConfig(
                url=HttpUrl("https://x/v1/"),
                api_key=SecretStr("sk-x"),
                flavor=OpenChatFlavor.OPENAI,
            ),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="OpenRouterConfig"):
            OpenRouterLLM(provider)

    def test_logs_init(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="primer.llm.openrouter")
        OpenRouterLLM(_make_provider(app_name="p"))
        records = [r for r in caplog.records if "OpenRouter adapter initialized" in r.message]
        assert len(records) == 1
        assert records[0].app_name_set is True  # type: ignore[attr-defined]


class TestGetClient:
    async def test_base_url_pinned(self) -> None:
        llm = OpenRouterLLM(_make_provider())
        try:
            client = llm._get_client()
            assert str(client.base_url).rstrip("/") == OPENROUTER_BASE_URL.rstrip("/")
        finally:
            await llm.aclose()

    async def test_client_cached(self) -> None:
        llm = OpenRouterLLM(_make_provider())
        try:
            assert llm._get_client() is llm._get_client()
        finally:
            await llm.aclose()


class TestListModels:
    async def test_sorted_dedup(self) -> None:
        llm = OpenRouterLLM(_make_provider(models=["openai/gpt-4o", "anthropic/claude-3.5-sonnet"]))
        try:
            assert list(await llm.list_models()) == [
                "anthropic/claude-3.5-sonnet",
                "openai/gpt-4o",
            ]
        finally:
            await llm.aclose()

    async def test_no_upstream_call(self) -> None:
        llm = OpenRouterLLM(_make_provider())
        try:
            with respx.mock(assert_all_called=False) as router:
                router.get(f"{OPENROUTER_BASE_URL}/models").mock(
                    return_value=httpx.Response(500)
                )
                assert list(await llm.list_models()) == ["anthropic/claude-3.5-sonnet"]
        finally:
            await llm.aclose()


class TestCountTokens:
    async def test_positive(self) -> None:
        llm = OpenRouterLLM(_make_provider())
        try:
            n = await llm.count_tokens(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi there")])],
                tools=None,
            )
            assert isinstance(n, int) and n > 0
        finally:
            await llm.aclose()


class TestStream:
    async def test_unknown_model_raises(self) -> None:
        llm = OpenRouterLLM(_make_provider(models=["anthropic/claude-3.5-sonnet"]))
        try:
            with pytest.raises(ModelNotFoundError, match="nope"):
                async for _ in llm.stream(
                    model="nope",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                ):
                    pass
        finally:
            await llm.aclose()

    @respx.mock
    async def test_happy_path_events(self) -> None:
        _mock_stream()
        llm = OpenRouterLLM(_make_provider())
        try:
            events = [
                ev
                async for ev in llm.stream(
                    model="anthropic/claude-3.5-sonnet",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                )
            ]
        finally:
            await llm.aclose()
        assert any(isinstance(e, StreamStart) for e in events)
        assert any(isinstance(e, TextDelta) and e.text == "hello" for e in events)
        assert any(isinstance(e, Usage) for e in events)
        assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"

    @respx.mock
    async def test_attribution_and_auth_headers_sent(self) -> None:
        _mock_stream()
        llm = OpenRouterLLM(
            _make_provider(api_key="sk-or-v1-zzz", app_name="primer", app_url="https://p.example")
        )
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
            req = respx.calls.last.request
            assert req.headers["Authorization"] == "Bearer sk-or-v1-zzz"
            assert req.headers["X-Title"] == "primer"
            assert req.headers["HTTP-Referer"] == "https://p.example/"
        finally:
            await llm.aclose()

    @respx.mock
    async def test_4xx_surfaces_as_bad_request(self) -> None:
        respx.post(f"{OPENROUTER_BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                400,
                json={"error": {"message": "bad model id", "code": "invalid_request_error"}},
            )
        )
        llm = OpenRouterLLM(_make_provider())
        try:
            with pytest.raises(BadRequestError):
                async for _ in llm.stream(
                    model="anthropic/claude-3.5-sonnet",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                ):
                    pass
        finally:
            await llm.aclose()

    @respx.mock
    async def test_request_body_includes_tools_and_response_format(self) -> None:
        _mock_stream()

        class Out(PydanticBaseModel):
            value: int

        tool = Tool(
            id="search",
            description="Search",
            toolset_id="default",
            args_schema={"type": "object", "properties": {}, "required": []},
        )
        llm = OpenRouterLLM(_make_provider())
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
                temperature=0.3,
                max_output_tokens=64,
                stop=["END"],
                tools=[tool],
                tool_choice="required",
                response_format=Out,
                extended={"seed": 5, "junk": 1},
            ):
                pass
        finally:
            await llm.aclose()
        body = json.loads(respx.calls.last.request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["max_tokens"] == 64
        assert body["stop"] == ["END"]
        assert body["tools"][0]["function"]["name"] == "search"
        assert body["tool_choice"] == "required"
        assert body["response_format"]["json_schema"]["name"] == "Out"
        assert body["seed"] == 5
        assert "junk" not in body

    @respx.mock
    async def test_trace_llm_io_records_messages(self) -> None:
        _mock_stream()
        llm = OpenRouterLLM(_make_provider(), trace_llm_io=True)
        try:
            events = [
                ev
                async for ev in llm.stream(
                    model="anthropic/claude-3.5-sonnet",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                    max_output_tokens=32,
                )
            ]
        finally:
            await llm.aclose()
        assert any(isinstance(e, Done) for e in events)

    @respx.mock
    async def test_generation_budget_maps_to_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from primer.llm._timeout import GenerationBudgetExceeded

        _mock_stream()

        async def budget_iter(*_a, **_k):
            raise GenerationBudgetExceeded("over")
            yield  # pragma: no cover

        monkeypatch.setattr("primer.llm.openrouter._iter_with_timeout", budget_iter)
        llm = OpenRouterLLM(_make_provider(total_timeout_seconds=30.0))
        try:
            with pytest.raises(ProviderTimeoutError) as info:
                async for _ in llm.stream(
                    model="anthropic/claude-3.5-sonnet",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                ):
                    pass
            assert info.value.code == "generation_timeout"
        finally:
            await llm.aclose()

    @respx.mock
    async def test_stall_timeout_maps_to_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_stream()

        async def stall_iter(*_a, **_k):
            raise TimeoutError("stall")
            yield  # pragma: no cover

        monkeypatch.setattr("primer.llm.openrouter._iter_with_timeout", stall_iter)
        llm = OpenRouterLLM(_make_provider())
        try:
            with pytest.raises(ProviderTimeoutError) as info:
                async for _ in llm.stream(
                    model="anthropic/claude-3.5-sonnet",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                ):
                    pass
            assert info.value.code == "stream_timeout"
        finally:
            await llm.aclose()

    @respx.mock
    async def test_mid_stream_error_yields_chat_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace as NS

        _mock_stream()

        async def failing_iter(*_a, **_k):
            yield NS(
                id="x",
                model="m",
                choices=[
                    NS(
                        index=0,
                        delta=NS(role="assistant", content=None, tool_calls=None),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            raise RuntimeError("boom mid-stream")

        monkeypatch.setattr("primer.llm.openrouter._iter_with_timeout", failing_iter)
        llm = OpenRouterLLM(_make_provider())
        try:
            events = [
                ev
                async for ev in llm.stream(
                    model="anthropic/claude-3.5-sonnet",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                )
            ]
        finally:
            await llm.aclose()
        assert isinstance(events[0], StreamStart)
        assert isinstance(events[-1], ChatError) and events[-1].fatal is True


class TestAclose:
    async def test_idempotent(self) -> None:
        llm = OpenRouterLLM(_make_provider())
        llm._get_client()
        await llm.aclose()
        await llm.aclose()
        assert llm._client is None


class TestDiscover:
    @respx.mock
    async def test_rich_catalogue(self) -> None:
        respx.get(f"{OPENROUTER_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "anthropic/claude-3.5-sonnet",
                            "name": "Claude 3.5 Sonnet",
                            "context_length": 200000,
                            "pricing": {"prompt": "3", "completion": "15"},
                            "architecture": {"modality": "text"},
                        },
                        "not-a-dict",
                        {"name": "no id here"},
                    ]
                },
            )
        )
        out = await _discover_openrouter_models(OpenRouterConfig(api_key=SecretStr("sk-or-v1-abc")))
        assert len(out) == 1
        row = out[0]
        assert row["id"] == "anthropic/claude-3.5-sonnet"
        assert row["input_price_per_million"] == "3"
        assert row["output_price_per_million"] == "15"
        assert row["modality"] == "text"

    @respx.mock
    async def test_missing_fields_default(self) -> None:
        respx.get(f"{OPENROUTER_BASE_URL}/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "some/model"}]})
        )
        out = await _discover_openrouter_models(OpenRouterConfig(api_key=SecretStr("sk-or-v1-abc")))
        row = out[0]
        assert row["name"] == "some/model"
        assert row["context_length"] is None
        assert row["input_price_per_million"] is None
        assert row["modality"] == "text"

    @respx.mock
    async def test_empty_data(self) -> None:
        respx.get(f"{OPENROUTER_BASE_URL}/models").mock(
            return_value=httpx.Response(200, json={})
        )
        assert await _discover_openrouter_models(OpenRouterConfig(api_key=SecretStr("k"))) == []

    @respx.mock
    async def test_4xx_raises_http_status_error(self) -> None:
        respx.get(f"{OPENROUTER_BASE_URL}/models").mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
        )
        with pytest.raises(httpx.HTTPStatusError) as info:
            await _discover_openrouter_models(OpenRouterConfig(api_key=SecretStr("sk-or-v1-bad")))
        assert info.value.response.status_code == 401

    @respx.mock
    async def test_discover_sends_attribution(self) -> None:
        route = respx.get(f"{OPENROUTER_BASE_URL}/models").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        await _discover_openrouter_models(
            OpenRouterConfig(api_key=SecretStr("k"), app_name="primer")
        )
        assert route.calls.last.request.headers["X-Title"] == "primer"
        assert route.calls.last.request.headers["Authorization"] == "Bearer k"
