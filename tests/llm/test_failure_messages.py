"""A failed model call says which provider failed and what the provider said (C-024, ticket 01a11d23-8e66).

Every llm adapter turned an SDK exception into a fixed label ("OpenAI server error") that dropped the provider's own message and named OpenAI for
any OpenAI-compatible endpoint (LM Studio, vLLM, a proxy). The classification code (``server_error``, ``rate_limit``...) is what the console's
failure words read and stays as it was; the MESSAGE now names the provider by its configured id and kind, carries the provider's own text capped,
and has credentials masked out of it (the configured API key, the Base URL's password, URL-borne secrets).

The SDK exceptions here are the real ones: the openai and anthropic clients run over an in-process transport so ``body`` has the shape the SDK
gives it, and gemini and ollama exceptions are built with their real constructors.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import google.genai.errors as gerrors
import ollama
import openai
import pytest
from pydantic import HttpUrl, SecretStr

from primer.llm.anthropic import AnthropicLLM
from primer.llm.gemini import GeminiLLM
from primer.llm.ollama import OllamaLLM
from primer.llm.openchat import OpenChatLLM
from primer.llm.openresponses import OpenResponsesLLM
from primer.llm.openrouter import OpenRouterLLM
from primer.model.chat import Error as ChatError, Message, TextPart
from primer.model.except_ import PrimerError
from primer.model.provider import (
    AnthropicConfig,
    GoogleConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    OllamaConfig,
    OpenChatConfig,
    OpenChatFlavor,
    OpenResponsesConfig,
    OpenResponsesFlavor,
    OpenRouterConfig,
)
from tests.llm.test_openrouter import _sdk_http_module

PROVIDER_ID = "lm-studio-box"
API_KEY = "sk-live-1234567890abcdef"
BASE_URL_PASSWORD = "hunter2-base-url"
UPSTREAM = "upstream exploded"
MESSAGES = [Message(role="user", parts=[TextPart(text="hi")])]


# ---- real SDK exceptions ---------------------------------------------------------------------------------------------------------------------


async def _openai_sdk_error(status: int, payload: Any) -> openai.APIStatusError:
    """What the openai client raises for a response with ``status`` and ``payload`` (a dict is sent as JSON, a str as the raw body)."""
    http = _sdk_http_module()

    def handler(request):
        return http.Response(status, json=payload) if not isinstance(payload, str) else http.Response(status, text=payload)

    client = openai.AsyncOpenAI(
        api_key="k", base_url="http://sdk.test/v1", max_retries=0, http_client=http.AsyncClient(transport=http.MockTransport(handler)),
    )
    with pytest.raises(openai.APIStatusError) as raised:
        await client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    return raised.value


async def _anthropic_sdk_error(status: int, payload: Any) -> anthropic.APIStatusError:
    http = _sdk_http_module()

    def handler(request):
        return http.Response(status, json=payload)

    client = anthropic.AsyncAnthropic(
        api_key="k", base_url="http://sdk.test", max_retries=0, http_client=http.AsyncClient(transport=http.MockTransport(handler)),
    )
    with pytest.raises(anthropic.APIStatusError) as raised:
        await client.messages.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "hi"}])
    return raised.value


# ---- the six adapters, each failing its first call with a 500 carrying ``text`` --------------------------------------------------------------


@dataclass
class _Adapter:
    kind: str                                   # what the label must say about the backend
    build: Callable[..., Awaitable[tuple[Any, str]]]   # (monkeypatch, text, url_password, site) -> (llm, model)
    has_url: bool


def _failing_stream(exc: Exception):
    """A stream that raises ``exc`` on its FIRST iteration: how google-genai and ollama open a request (lazily), and how any adapter's stream
    fails before its first event."""
    async def gen():
        raise exc
        yield  # pragma: no cover

    return gen()


def _raising(exc: Exception, site: str) -> AsyncMock:
    """The SDK call that fails: at the call itself (``open``: the request is refused before a stream exists) or on the first iteration of the
    stream it returns (``stream``: the in-stream site of the adapter)."""
    return AsyncMock(side_effect=exc) if site == "open" else AsyncMock(return_value=_failing_stream(exc))


def _provider(provider_type: LLMProviderType, config) -> LLMProvider:
    return LLMProvider(id=PROVIDER_ID, provider=provider_type, config=config, limits=Limits(max_concurrency=4))


def _url(password: bool) -> HttpUrl:
    userinfo = f"user:{BASE_URL_PASSWORD}@" if password else ""
    return HttpUrl(f"http://{userinfo}lmstudio.local:1234/v1")


async def _openchat(monkeypatch, text, url_password, site):
    provider = _provider(
        LLMProviderType.OPENCHAT,
        OpenChatConfig(url=_url(url_password), api_key=SecretStr(API_KEY), flavor=OpenChatFlavor.LMSTUDIO),
    )
    client = MagicMock()
    client.chat.completions.create = _raising(await _openai_sdk_error(500, {"error": {"message": text}}), site)
    monkeypatch.setattr("primer.llm.openchat.AsyncOpenAI", MagicMock(return_value=client))
    return OpenChatLLM(provider), "qwen"


async def _openresponses(monkeypatch, text, url_password, site):
    provider = _provider(
        LLMProviderType.OPENRESPONSES,
        OpenResponsesConfig(url=_url(url_password), api_key=SecretStr(API_KEY), flavor=OpenResponsesFlavor.LMSTUDIO),
    )
    client = MagicMock()
    client.responses.create = _raising(await _openai_sdk_error(500, {"error": {"message": text}}), site)
    monkeypatch.setattr("primer.llm.openresponses.AsyncOpenAI", MagicMock(return_value=client))
    return OpenResponsesLLM(provider), "qwen"


async def _openrouter(monkeypatch, text, url_password, site):
    provider = _provider(LLMProviderType.OPENROUTER, OpenRouterConfig(api_key=SecretStr(API_KEY)))
    client = MagicMock()
    client.chat.completions.create = _raising(await _openai_sdk_error(500, {"error": {"message": text}}), site)
    monkeypatch.setattr("primer.llm.openrouter.AsyncOpenAI", MagicMock(return_value=client))
    return OpenRouterLLM(provider), "anthropic/claude"


async def _anthropic(monkeypatch, text, url_password, site):
    provider = _provider(LLMProviderType.ANTHROPIC, AnthropicConfig(api_key=SecretStr(API_KEY)))
    client = MagicMock()
    error = await _anthropic_sdk_error(500, {"type": "error", "error": {"type": "api_error", "message": text}})
    client.messages.create = _raising(error, site)
    monkeypatch.setattr("primer.llm.anthropic.AsyncAnthropic", MagicMock(return_value=client))
    return AnthropicLLM(provider), "claude-sonnet-4-5"


async def _gemini(monkeypatch, text, url_password, site):
    provider = _provider(LLMProviderType.GEMINI, GoogleConfig(api_key=SecretStr(API_KEY)))
    client = MagicMock()
    error = gerrors.ServerError(500, {"error": {"code": 500, "message": text, "status": "INTERNAL"}})
    client.aio.models.generate_content_stream = _raising(error, site)
    monkeypatch.setattr("primer.llm.gemini.genai.Client", MagicMock(return_value=client))
    return GeminiLLM(provider), "gemini-2.5-flash"


async def _ollama(monkeypatch, text, url_password, site):
    provider = _provider(LLMProviderType.OLLAMA, OllamaConfig(url=_url(url_password), api_key=SecretStr(API_KEY)))
    client = MagicMock()
    client.chat = _raising(ollama.ResponseError(text, 500), site)
    monkeypatch.setattr("primer.llm.ollama.ollama.AsyncClient", MagicMock(return_value=client))
    return OllamaLLM(provider), "llama3"


ADAPTERS = {
    "openchat": _Adapter("openchat", _openchat, True),
    "openresponses": _Adapter("openresponses", _openresponses, True),
    "openrouter": _Adapter("openrouter", _openrouter, False),
    "anthropic": _Adapter("anthropic", _anthropic, False),
    "gemini": _Adapter("gemini", _gemini, False),
    "ollama": _Adapter("ollama", _ollama, True),
}
OPENAI_COMPATIBLE = ("openchat", "openresponses", "openrouter")


SITES = ("open", "stream")
#: (adapter, site) for every call site of ``describe_failure``: the request refused before a stream exists, and a stream that fails on its first
#: iteration. google-genai and ollama open a request lazily, so for them the second is the production shape of a real 5xx, 429 or 401.
CASES = [pytest.param(name, site, id=f"{name}-{site}") for name in ADAPTERS for site in SITES]


async def _failure(adapter: str, monkeypatch, text: str, *, url_password: bool = False, site: str = "open") -> tuple[str, str | None]:
    """The (message, code) a failed first call surfaces: the exception an adapter raises, or the terminal Error it yields."""
    llm, model = await ADAPTERS[adapter].build(monkeypatch, text, url_password, site)
    kwargs: dict[str, Any] = {"model": model, "messages": MESSAGES}
    if adapter == "anthropic":
        kwargs["max_output_tokens"] = 64
    try:
        events = [event async for event in llm.stream(**kwargs)]
    except PrimerError as err:
        return err.message, err.code
    last = events[-1]
    assert isinstance(last, ChatError), events
    return last.message, last.code


@pytest.mark.parametrize("adapter, site", CASES)
async def test_a_server_error_names_the_provider_and_carries_the_providers_own_text(adapter, site, monkeypatch):
    message, code = await _failure(adapter, monkeypatch, UPSTREAM, site=site)

    assert code == "server_error", "the classification the console's failure words read is unchanged"
    assert PROVIDER_ID in message, message
    assert ADAPTERS[adapter].kind in message, message
    assert UPSTREAM in message, message
    assert "HTTP 500" in message, message


@pytest.mark.parametrize("adapter, site", [c for c in CASES if c.values[0] in OPENAI_COMPATIBLE])
async def test_an_openai_compatible_endpoint_is_not_called_openai(adapter, site, monkeypatch):
    message, _ = await _failure(adapter, monkeypatch, UPSTREAM, site=site)

    assert "openai" not in message.lower(), message


@pytest.mark.parametrize("adapter, site", CASES)
async def test_the_providers_text_is_capped(adapter, site, monkeypatch):
    message, _ = await _failure(adapter, monkeypatch, "boom " * 2000, site=site)

    assert len(message) < 600, len(message)
    assert PROVIDER_ID in message and message.rstrip().endswith("..."), message


@pytest.mark.parametrize("adapter, site", CASES)
async def test_the_configured_api_key_echoed_by_the_provider_is_masked(adapter, site, monkeypatch):
    message, _ = await _failure(adapter, monkeypatch, f"invalid key {API_KEY} rejected", site=site)

    assert API_KEY not in message, message
    assert "invalid key" in message and "rejected" in message, "only the secret is masked, not the provider's sentence around it"


@pytest.mark.parametrize("adapter, site", [c for c in CASES if ADAPTERS[c.values[0]].has_url])
async def test_a_base_url_password_echoed_by_the_provider_is_masked(adapter, site, monkeypatch):
    text = f"cannot reach http://user:{BASE_URL_PASSWORD}@lmstudio.local:1234/v1/chat (connection refused)"
    message, _ = await _failure(adapter, monkeypatch, text, url_password=True, site=site)

    assert BASE_URL_PASSWORD not in message, message
    assert "lmstudio.local" in message, message


# ---- a hostile body, through the real adapters ---------------------------------------------------------------------------------------------------


def _nested(depth: int) -> dict:
    """``{"error": {"error": ... {"code": "x"}}}``, built iteratively so building it needs no recursion."""
    body: dict = {"code": "x"}
    for _ in range(depth):
        body = {"error": body}
    return body


def _hostile(cls: type, depth: int):
    """The SDK's own exception class carrying a body nested ``depth`` levels deep (a decoded JSON body can nest to the recursion limit)."""
    exc = cls.__new__(cls)
    exc.status_code, exc.code, exc.message, exc.body = 500, None, "Error code: 500", _nested(depth)
    Exception.__init__(exc, exc.message)
    return exc


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("adapter, cls", [("openchat", openai.InternalServerError), ("anthropic", anthropic.InternalServerError)])
async def test_a_hostile_nested_body_is_still_a_classified_retryable_failure(adapter, cls, site, monkeypatch):
    """A 500 whose body nests ``{"error": ...}`` thousands of levels deep used to blow the stack inside the adapter's except: the turn ended as
    an internal error, was not retryable, and the traceback logged the raw body."""
    from primer.llm._retry import is_retryable
    from primer.model.except_ import ServerError

    exc = _hostile(cls, 5000)
    if adapter == "openchat":
        provider = _provider(LLMProviderType.OPENCHAT, OpenChatConfig(url=_url(False), api_key=SecretStr(API_KEY), flavor=OpenChatFlavor.LMSTUDIO))
        client = MagicMock()
        client.chat.completions.create = _raising(exc, site)
        monkeypatch.setattr("primer.llm.openchat.AsyncOpenAI", MagicMock(return_value=client))
        llm, model, kwargs = OpenChatLLM(provider), "qwen", {}
    else:
        provider = _provider(LLMProviderType.ANTHROPIC, AnthropicConfig(api_key=SecretStr(API_KEY)))
        client = MagicMock()
        client.messages.create = _raising(exc, site)
        monkeypatch.setattr("primer.llm.anthropic.AsyncAnthropic", MagicMock(return_value=client))
        llm, model, kwargs = AnthropicLLM(provider), "claude-sonnet-4-5", {"max_output_tokens": 64}

    try:
        events = [e async for e in llm.stream(model=model, messages=MESSAGES, **kwargs)]
    except ServerError as err:
        assert is_retryable(err) and err.code == "server_error"
        message = err.message
    else:
        last = events[-1]
        assert isinstance(last, ChatError) and last.code == "server_error", events
        message = last.message

    assert message.startswith(f"Model provider '{PROVIDER_ID}' ") and "had a server error (HTTP 500)" in message, message
    assert len(message) < 600
