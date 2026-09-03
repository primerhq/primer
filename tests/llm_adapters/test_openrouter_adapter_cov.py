"""Coverage tests for the OpenRouterLLM adapter.

Placed outside ``tests/llm/`` so ``primer.llm.openrouter`` counts in the
CI unit sweep.

``TestDiscover`` mocks with respx: ``_discover_openrouter_models`` uses a
plain ``httpx.AsyncClient`` directly (not the openai SDK), so respx's
``httpx``-transport patching still works.

``TestStream`` instead monkeypatches ``primer.llm.openrouter.AsyncOpenAI``
itself (mirroring ``tests/llm/test_openresponses.py``'s ``_patched_client``
pattern) rather than mocking at the HTTP layer. openai 3.x's SDK client
now defaults to an internal ``httpx2.AsyncClient`` (a separate package
from ``httpx``, see ``openai._base_client.AsyncAPIClient``) that respx
cannot intercept -- every TestStream test that respx-mocked the wire
silently stopped being mocked at all and started making a real network
call to OpenRouter, which correctly 401'd. Mocking the SDK client
construction instead is transport-agnostic by construction: it never
cares whether the SDK's own default is httpx or httpx2.
"""

from __future__ import annotations

from primer.model_profile import ResolvedModel
from primer.model.model_profile import ModelProfileConfig

import json
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
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
            ResolvedModel(profile_id="test-profile", provider_id="test-provider", model_name=n, context_length=200000, config=ModelProfileConfig())
            for n in (models or ["anthropic/claude-3.5-sonnet"])
        ],
        limits=Limits(
            max_concurrency=4,
            total_timeout_seconds=total_timeout_seconds,
        ),
    )


def _chunk(
    *,
    delta_role: str | None = None,
    delta_content: str | None = None,
    finish_reason: str | None = None,
    usage: NS | None = None,
) -> NS:
    """One duck-typed Chat Completions streaming chunk.

    ``_translate_chunk`` (primer/llm/_openai_compat.py) reads every field
    via ``getattr(x, attr, None)``, so a plain SimpleNamespace is exactly
    as good a stand-in as a real ``openai.types.chat.ChatCompletionChunk``
    -- test_mid_stream_error_yields_chat_error already relied on this same
    duck-typing before this migration.
    """
    return NS(
        id="x",
        model="anthropic/claude-3.5-sonnet",
        choices=[
            NS(
                index=0,
                delta=NS(
                    role=delta_role, content=delta_content, tool_calls=None,
                    reasoning_content=None, reasoning=None,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )


# Same three-chunk shape the old SSE fixture encoded: a role-opening
# delta, a content delta ("hello"), then a stop + usage chunk.
_HAPPY_CHUNKS: list[NS] = [
    _chunk(delta_role="assistant"),
    _chunk(delta_content="hello"),
    _chunk(
        finish_reason="stop",
        usage=NS(prompt_tokens=1, completion_tokens=2, total_tokens=3),
    ),
]


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


def _make_openai_error(cls: type, *, status_code: int = 400, code: str | None = None):
    """Build an openai SDK exception with minimal init plumbing.

    The SDK's exception constructors require a Response and body in real
    use; bypass __init__ and set the attributes classify_openai_exception
    actually reads. Same name/shape as tests/llm/test_openresponses.py's
    helper for the sibling adapter -- duplicated locally rather than
    imported, matching this test suite's convention of self-contained
    adapter test files.
    """
    exc = cls.__new__(cls)
    exc.status_code = status_code
    exc.code = code
    exc.message = f"test {cls.__name__}"
    Exception.__init__(exc, exc.message)
    return exc


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the AsyncOpenAI symbol in the adapter module to a MagicMock.

    Returns the mock instance the adapter will see when it constructs
    the client (OpenRouterLLM._get_client). Tests configure
    ``mock.chat.completions.create`` to drive the SDK behaviour, and can
    inspect ``mock.ctor_mock.call_args`` to assert what the adapter
    passed to the constructor (api_key, default_headers) -- the
    boundary the adapter code actually controls, in place of the old
    respx-captured wire headers. ``close`` is also an AsyncMock --
    OpenRouterLLM.aclose() awaits it, and a plain MagicMock() default
    isn't awaitable.
    """
    mock_instance = MagicMock()
    mock_instance.chat = MagicMock()
    mock_instance.chat.completions = MagicMock()
    mock_instance.chat.completions.create = AsyncMock()
    mock_instance.close = AsyncMock()
    cls_mock = MagicMock(return_value=mock_instance)
    mock_instance.ctor_mock = cls_mock
    monkeypatch.setattr("primer.llm.openrouter.AsyncOpenAI", cls_mock)
    return mock_instance


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
    async def test_happy_path_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_HAPPY_CHUNKS)
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

    async def test_attribution_and_auth_headers_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_HAPPY_CHUNKS)
        llm = OpenRouterLLM(
            _make_provider(api_key="sk-or-v1-zzz", app_name="primer", app_url="https://p.example")
        )
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
            # Assert at the boundary the adapter itself controls: what it
            # passed to construct the SDK client. Whether the SDK then
            # correctly turns default_headers/api_key into wire headers
            # is the SDK's own tested responsibility, not this adapter's.
            ctor_kwargs = client.ctor_mock.call_args.kwargs
            assert ctor_kwargs["api_key"] == "sk-or-v1-zzz"
            assert ctor_kwargs["default_headers"] == {
                "X-Title": "primer",
                "HTTP-Referer": "https://p.example/",
            }
        finally:
            await llm.aclose()

    async def test_4xx_surfaces_as_bad_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _patched_client(monkeypatch)
        client.chat.completions.create.side_effect = _make_openai_error(
            openai.BadRequestError, status_code=400, code="invalid_request_error",
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

    async def test_request_body_includes_tools_and_response_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_HAPPY_CHUNKS)

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
        # The exact dict passed to create(**request) -- what the old
        # respx assertion reconstructed by re-parsing the JSON the SDK
        # put on the wire, without the SDK-serialisation round-trip.
        body = client.chat.completions.create.call_args.kwargs
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["max_tokens"] == 64
        assert body["stop"] == ["END"]
        assert body["tools"][0]["function"]["name"] == "search"
        assert body["tool_choice"] == "required"
        assert body["response_format"]["json_schema"]["name"] == "Out"
        assert body["seed"] == 5
        assert "junk" not in body

    async def test_trace_llm_io_records_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_HAPPY_CHUNKS)
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

    async def test_generation_budget_maps_to_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from primer.llm._timeout import GenerationBudgetExceeded

        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_HAPPY_CHUNKS)

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

    async def test_stall_timeout_maps_to_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_HAPPY_CHUNKS)

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

    async def test_mid_stream_error_yields_chat_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_HAPPY_CHUNKS)

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
