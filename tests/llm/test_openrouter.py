"""Tests for OpenRouterLLM.

The adapter wraps the openai Python SDK pointed at OpenRouter's
base URL. The openai SDK's transport is mocked via respx so the
tests are pure in-process; no network IO.

Spec: docs/superpowers/specs/2026-06-04-openrouter-llm-provider-design.md
"""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from primer.llm.openrouter import (
    OPENROUTER_BASE_URL,
    OpenRouterLLM,
    _discover_openrouter_models,
)
from primer.model.chat import Message, TextPart
from primer.model.except_ import BadRequestError
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
) -> LLMProvider:
    return LLMProvider(
        id="or-1",
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
        limits=Limits(max_concurrency=4),
    )


# --- Test cases follow ---


class TestClientConstruction:
    """1. The openai SDK client is constructed with OpenRouter's base URL."""

    async def test_base_url_pins_openrouter(self) -> None:
        llm = OpenRouterLLM(_make_provider())
        try:
            client = llm._get_client()
            assert str(client.base_url).rstrip("/") == OPENROUTER_BASE_URL.rstrip("/")
        finally:
            await llm.aclose()


class TestAttributionHeaders:
    """2-4. X-Title and HTTP-Referer header configurations."""

    @respx.mock
    async def test_both_attribution_fields_set(self) -> None:
        respx.post(f"{OPENROUTER_BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            ),
        )
        llm = OpenRouterLLM(_make_provider(
            app_name="primer-staging", app_url="https://primer.example",
        ))
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
            req = respx.calls.last.request
            assert req.headers.get("X-Title") == "primer-staging"
            # HttpUrl normalises to trailing slash; OpenRouter accepts either.
            assert req.headers.get("HTTP-Referer") == "https://primer.example/"
        finally:
            await llm.aclose()

    @respx.mock
    async def test_only_app_name_set(self) -> None:
        respx.post(f"{OPENROUTER_BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            ),
        )
        llm = OpenRouterLLM(_make_provider(app_name="primer-staging"))
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
            req = respx.calls.last.request
            assert req.headers.get("X-Title") == "primer-staging"
            assert req.headers.get("HTTP-Referer") is None
        finally:
            await llm.aclose()

    @respx.mock
    async def test_neither_attribution_field_set(self) -> None:
        respx.post(f"{OPENROUTER_BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            ),
        )
        llm = OpenRouterLLM(_make_provider())
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
            req = respx.calls.last.request
            assert req.headers.get("X-Title") is None
            assert req.headers.get("HTTP-Referer") is None
        finally:
            await llm.aclose()


class TestAuth:
    """5. Authorization: Bearer <key> on every request."""

    @respx.mock
    async def test_authorization_bearer_sent(self) -> None:
        respx.post(f"{OPENROUTER_BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            ),
        )
        llm = OpenRouterLLM(_make_provider(api_key="sk-or-v1-zzz"))
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
            req = respx.calls.last.request
            assert req.headers["Authorization"] == "Bearer sk-or-v1-zzz"
        finally:
            await llm.aclose()


class TestListModels:
    """6-7. list_models returns configured-only and does not hit upstream."""

    async def test_returns_configured_models_sorted_dedup(self) -> None:
        # Plan §5.5: return the configured slugs verbatim, deduplicated, sorted.
        # The LLMProvider model's `models` field rejects duplicates (it's a
        # list[LLMModel], one per slug). Build with two distinct slugs and
        # confirm both come back in sorted order.
        llm = OpenRouterLLM(_make_provider(models=[
            "openai/gpt-4o",
            "anthropic/claude-3.5-sonnet",
        ]))
        try:
            out = list(await llm.list_models())
            assert out == ["anthropic/claude-3.5-sonnet", "openai/gpt-4o"]
        finally:
            await llm.aclose()

    async def test_does_not_hit_upstream(self) -> None:
        # If list_models() opened a network call, respx would 500 the URL
        # and the call would raise. The assertion is that list_models()
        # returns cleanly because it never makes the call.
        llm = OpenRouterLLM(_make_provider())
        try:
            with respx.mock(assert_all_called=False) as router:
                router.get(f"{OPENROUTER_BASE_URL}/models").mock(
                    return_value=httpx.Response(500),
                )
                out = list(await llm.list_models())
                assert out == ["anthropic/claude-3.5-sonnet"]
        finally:
            await llm.aclose()


class TestCountTokens:
    """8. count_tokens returns a non-zero integer (approximation via tiktoken)."""

    async def test_returns_nonzero_integer(self) -> None:
        llm = OpenRouterLLM(_make_provider())
        try:
            n = await llm.count_tokens(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hello world")])],
                tools=None,
            )
            assert isinstance(n, int) and n > 0
        finally:
            await llm.aclose()


class TestStream:
    """9-10. stream() happy path + error envelope."""

    @respx.mock
    async def test_happy_path_emits_events(self) -> None:
        sse = (
            b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
            b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
            b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n\n'
            b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
            b"data: [DONE]\n\n"
        )
        respx.post(f"{OPENROUTER_BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200, content=sse,
                headers={"content-type": "text/event-stream"},
            ),
        )
        llm = OpenRouterLLM(_make_provider())
        try:
            events = []
            async for ev in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                events.append(ev)
            # At least StreamStart + one TextDelta should arrive.
            assert len(events) >= 2
        finally:
            await llm.aclose()

    @respx.mock
    async def test_4xx_surfaces_as_provider_error(self) -> None:
        # OpenRouter (like OpenAI) returns `code` as a string slug
        # ("invalid_request_error"), not an int. The integer status is
        # carried in the HTTP response status itself.
        respx.post(f"{OPENROUTER_BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                400,
                json={"error": {
                    "message": "bad model id",
                    "code": "invalid_request_error",
                }},
            ),
        )
        llm = OpenRouterLLM(_make_provider())
        try:
            with pytest.raises(BadRequestError) as exc_info:
                async for _ in llm.stream(
                    model="anthropic/claude-3.5-sonnet",
                    messages=[Message(role="user", parts=[TextPart(text="hi")])],
                ):
                    pass
            assert (
                "bad model id" in str(exc_info.value).lower()
                or "400" in str(exc_info.value)
            )
        finally:
            await llm.aclose()


class TestAclose:
    """11. aclose() closes the openai SDK client and is idempotent."""

    async def test_idempotent(self) -> None:
        llm = OpenRouterLLM(_make_provider())
        llm._get_client()  # force construction
        await llm.aclose()
        await llm.aclose()  # second call must not raise


class TestDiscoverHelper:
    """12-13. _discover_openrouter_models parses the rich catalogue."""

    @respx.mock
    async def test_returns_rich_catalogue(self) -> None:
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
                        {
                            "id": "openai/gpt-4o",
                            "name": "GPT-4o",
                            "context_length": 128000,
                            "pricing": {"prompt": "5", "completion": "20"},
                            "architecture": {"modality": "text+image"},
                        },
                    ],
                },
            ),
        )
        out = await _discover_openrouter_models(
            OpenRouterConfig(api_key=SecretStr("sk-or-v1-abc")),
        )
        assert len(out) == 2
        first = out[0]
        assert first["id"] == "anthropic/claude-3.5-sonnet"
        assert first["name"] == "Claude 3.5 Sonnet"
        assert first["context_length"] == 200000
        assert first["input_price_per_million"] == "3"
        assert first["output_price_per_million"] == "15"
        assert first["modality"] == "text"

    @respx.mock
    async def test_missing_fields_default_gracefully(self) -> None:
        respx.get(f"{OPENROUTER_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "some/model"},  # no name, no pricing, no arch
                    ],
                },
            ),
        )
        out = await _discover_openrouter_models(
            OpenRouterConfig(api_key=SecretStr("sk-or-v1-abc")),
        )
        assert len(out) == 1
        row = out[0]
        assert row["id"] == "some/model"
        # The helper should fall back gracefully for missing fields.
        assert row.get("context_length") is None
        assert row.get("input_price_per_million") is None
        assert row.get("output_price_per_million") is None
        # modality has a default of "text" per spec §6.2
        assert row.get("modality") == "text"
