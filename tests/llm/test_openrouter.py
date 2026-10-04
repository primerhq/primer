"""Tests for OpenRouterLLM.

The adapter wraps the openai Python SDK pointed at OpenRouter's
base URL. The stream tests intercept at the SDK's own HTTP transport
(see ``wire`` below), so they exercise the real adapter + real SDK and
assert on the actual request that would hit the wire, with no network
IO. respx is only used for ``_discover_openrouter_models``, which talks
to plain ``httpx`` directly and not through the SDK.

Spec: docs/superpowers/specs/2026-06-04-openrouter-llm-provider-design.md
"""

from __future__ import annotations

from primer.model_profile import ResolvedModel
from primer.model.model_profile import ModelProfileConfig

import importlib

import httpx
import pytest
import respx
from openai import AsyncOpenAI
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
            ResolvedModel(profile_id="test-profile", provider_id="test-provider", model_name=n, context_length=200000, config=ModelProfileConfig())
            for n in (models or ["anthropic/claude-3.5-sonnet"])
        ],
        limits=Limits(max_concurrency=4),
    )


def _sdk_http_module():
    """The HTTP library the installed openai SDK builds its client on.

    openai 3.x moved from ``httpx`` to ``httpx2``. respx patches ``httpx``
    only, so against the 3.x SDK a ``@respx.mock`` test intercepts
    NOTHING: the request goes out to openrouter.ai for real, returns a
    401, and the test fails with an AuthenticationError that looks like a
    credentials problem rather than a missing mock. These tests were
    excluded from CI, so the SDK bump broke them unnoticed. Resolving the
    module from the SDK's own client keeps this correct on either side of
    that move instead of hard-coding one.
    """
    probe = AsyncOpenAI(api_key="probe")
    for cls in type(probe._client).__mro__:
        root = cls.__module__.split(".")[0]
        if root in ("httpx", "httpx2"):
            return importlib.import_module(root)
    raise RuntimeError(
        "openai SDK client is not built on httpx or httpx2; update this "
        "test helper to intercept its transport"
    )


class _Wire:
    """Captures every request the SDK sends and replies with a canned one."""

    def __init__(self, http) -> None:
        self.http = http
        self.requests: list = []
        self._response = None

    def respond(self, status: int = 200, **kwargs) -> None:
        self._response = (status, kwargs)

    def handle(self, request):
        self.requests.append(request)
        assert self._response is not None, "wire.respond() was never called"
        status, kwargs = self._response
        return self.http.Response(status, **kwargs)

    @property
    def last(self):
        assert self.requests, "the SDK sent no request through the wire"
        return self.requests[-1]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _Wire:
    """Route the adapter's openai client through an in-process transport.

    Replaces only the ``AsyncOpenAI`` symbol the adapter constructs from;
    the adapter's own ``_get_client`` still runs and supplies the real
    api_key / base_url / default_headers, so the captured request is what
    the adapter would actually send.
    """
    http = _sdk_http_module()
    w = _Wire(http)
    real = AsyncOpenAI

    def _factory(**kwargs):
        return real(
            **kwargs,
            http_client=http.AsyncClient(transport=http.MockTransport(w.handle)),
        )

    monkeypatch.setattr("primer.llm.openrouter.AsyncOpenAI", _factory)
    return w


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

    async def test_both_attribution_fields_set(self, wire: _Wire) -> None:
        wire.respond(
            200,
            content=b"data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
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
            req = wire.last
            assert req.headers.get("X-Title") == "primer-staging"
            # HttpUrl normalises to trailing slash; OpenRouter accepts either.
            assert req.headers.get("HTTP-Referer") == "https://primer.example/"
        finally:
            await llm.aclose()

    async def test_only_app_name_set(self, wire: _Wire) -> None:
        wire.respond(
            200,
            content=b"data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
        llm = OpenRouterLLM(_make_provider(app_name="primer-staging"))
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
            req = wire.last
            assert req.headers.get("X-Title") == "primer-staging"
            assert req.headers.get("HTTP-Referer") is None
        finally:
            await llm.aclose()

    async def test_neither_attribution_field_set(self, wire: _Wire) -> None:
        wire.respond(
            200,
            content=b"data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
        llm = OpenRouterLLM(_make_provider())
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
            req = wire.last
            assert req.headers.get("X-Title") is None
            assert req.headers.get("HTTP-Referer") is None
        finally:
            await llm.aclose()


class TestAuth:
    """5. Authorization: Bearer <key> on every request."""

    async def test_authorization_bearer_sent(self, wire: _Wire) -> None:
        wire.respond(
            200,
            content=b"data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
        llm = OpenRouterLLM(_make_provider(api_key="sk-or-v1-zzz"))
        try:
            async for _ in llm.stream(
                model="anthropic/claude-3.5-sonnet",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
            req = wire.last
            assert req.headers["Authorization"] == "Bearer sk-or-v1-zzz"
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

    async def test_happy_path_emits_events(self, wire: _Wire) -> None:
        sse = (
            b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
            b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
            b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n\n'
            b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
            b"data: [DONE]\n\n"
        )
        wire.respond(
            200, content=sse,
            headers={"content-type": "text/event-stream"},
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
            # Exactly one request, to OpenRouter's endpoint, and it went
            # through the in-process transport: the regression this
            # fixture exists to prevent is a mock that silently misses and
            # lets the request reach the real network.
            assert len(wire.requests) == 1
            assert str(wire.last.url) == f"{OPENROUTER_BASE_URL}/chat/completions"
        finally:
            await llm.aclose()

    async def test_4xx_surfaces_as_provider_error(self, wire: _Wire) -> None:
        # OpenRouter (like OpenAI) returns `code` as a string slug
        # ("invalid_request_error"), not an int. The integer status is
        # carried in the HTTP response status itself.
        wire.respond(
            400,
            json={"error": {
                "message": "bad model id",
                "code": "invalid_request_error",
            }},
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

    @pytest.mark.asyncio
    @respx.mock
    async def test_4xx_raises_http_status_error(self):
        """Pins the discover helper's error contract.

        OpenRouter's most common Fetch-Models failure mode is a bad
        API key, which returns 401. The helper calls raise_for_status,
        so callers see httpx.HTTPStatusError. The Phase 4 REST route
        wraps this into a structured response.
        """
        respx.get(f"{OPENROUTER_BASE_URL}/models").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"message": "invalid api key",
                                "code": "unauthorized"}},
            ),
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await _discover_openrouter_models(
                OpenRouterConfig(api_key=SecretStr("sk-or-v1-bad")),
            )
        assert exc_info.value.response.status_code == 401
