"""``describe_failure``: the message of a classified model-call failure (C-024, ticket 01a11d23-8e66).

The adapters call ``describe_failure(classify_*_exception(exc), exc, provider)``. It keeps the classification (class, ``code``, ``status_code``,
``cause``) and rewrites the MESSAGE of the four shapes that used to lose the provider's words (server error, rate limit, authentication, network);
a 4xx the provider rejected keeps its message, because ``primer.common.context_overflow`` reads it, and only has credentials masked out.
``test_failure_messages.py`` drives the same thing through the six adapters.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx
import openai
import pytest
from pydantic import HttpUrl, SecretStr

from primer.common.anthropic_errors import classify_anthropic_exception
from primer.common.context_overflow import is_context_overflow
from primer.common.openai_errors import classify_openai_exception
from primer.llm._failure import UPSTREAM_TEXT_CAP, describe_failure
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)
from tests.llm.test_failure_messages import _anthropic_sdk_error, _openai_sdk_error

API_KEY = "sk-live-1234567890abcdef"
LABEL = "Model provider 'lm-studio-box' (openchat/lmstudio)"


def _provider(url: str = "http://lmstudio.local:1234/v1") -> LLMProvider:
    return LLMProvider(
        id="lm-studio-box",
        provider=LLMProviderType.OPENCHAT,
        config=OpenChatConfig(url=HttpUrl(url), api_key=SecretStr(API_KEY), flavor=OpenChatFlavor.LMSTUDIO),
        limits=Limits(max_concurrency=1),
    )


def _described(exc, provider=None):
    return describe_failure(classify_openai_exception(exc), exc, provider or _provider())


async def test_a_server_error_reads_as_one_sentence_with_the_providers_words():
    exc = await _openai_sdk_error(500, {"error": {"message": "upstream exploded", "type": "server_error"}})

    err = _described(exc)

    assert isinstance(err, ServerError)
    assert err.message == f"{LABEL} had a server error (HTTP 500): upstream exploded"
    assert (err.code, err.status_code, err.cause) == ("server_error", 500, exc)


async def test_a_rate_limit_and_an_authentication_failure_say_so_with_their_own_text():
    limited = _described(await _openai_sdk_error(429, {"error": {"message": "Rate limit reached for requests"}}))
    rejected = _described(await _openai_sdk_error(401, {"error": {"message": "Invalid credentials"}}))

    assert isinstance(limited, RateLimitError) and limited.code == "rate_limit"
    assert limited.message == f"{LABEL} is rate limiting requests (HTTP 429): Rate limit reached for requests"
    assert isinstance(rejected, AuthenticationError) and rejected.code == "auth_error"
    assert rejected.message == f"{LABEL} rejected the credentials (HTTP 401): Invalid credentials"


async def test_anthropic_bodies_are_read_the_same_way():
    exc = await _anthropic_sdk_error(529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    provider = LLMProvider(
        id="ant-prod", provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr(API_KEY)), limits=Limits(max_concurrency=1),
    )

    err = describe_failure(classify_anthropic_exception(exc), exc, provider)

    assert isinstance(err, ServerError)
    assert err.message == "Model provider 'ant-prod' (anthropic) had a server error (HTTP 529): Overloaded"


async def test_a_body_that_is_not_json_is_a_bounded_slice_of_the_body():
    err = _described(await _openai_sdk_error(502, "<html>\n  <h1>Bad   Gateway</h1>\n</html>" + " padding" * 200))

    assert err.message.startswith(f"{LABEL} had a server error (HTTP 502): <html> <h1>Bad Gateway</h1> </html> padding")
    assert len(err.message) <= len(f"{LABEL} had a server error (HTTP 502): ") + UPSTREAM_TEXT_CAP
    assert err.message.endswith("...")


async def test_a_json_body_without_a_message_is_a_slice_of_the_json():
    err = _described(await _openai_sdk_error(500, {"error": {"code": "overloaded"}}))

    assert "overloaded" in err.message and err.message.startswith(f"{LABEL} had a server error (HTTP 500): ")


async def test_an_error_that_is_a_bare_string_is_that_string():
    err = _described(await _openai_sdk_error(500, {"error": "model crashed"}))

    assert err.message.endswith("(HTTP 500): model crashed")


async def test_no_body_text_leaves_the_sentence_without_a_trailing_colon():
    err = _described(await _openai_sdk_error(500, ""))

    assert err.message == f"{LABEL} had a server error (HTTP 500)"


async def test_a_dropped_connection_names_the_class_and_the_cause_with_credentials_masked():
    cause = httpx.ConnectError("[Errno 111] Connection refused (http://user:hunter2@lmstudio.local:1234/v1/chat)")
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://lmstudio.local:1234/v1/chat"))
    exc.__cause__ = cause

    err = _described(exc, _provider("http://user:hunter2@lmstudio.local:1234/v1"))

    assert isinstance(err, NetworkError) and err.code == "network_error"
    assert err.message.startswith(f"{LABEL} could not be reached (APIConnectionError): [Errno 111] Connection refused")
    assert "hunter2" not in err.message and "lmstudio.local" in err.message


async def test_a_request_the_provider_rejected_keeps_its_message_so_context_overflow_still_reads_it():
    body = {"error": {"message": "This model's maximum context length is 4096 tokens. However, you requested 9000 tokens."}}
    exc = await _openai_sdk_error(400, body)
    classified = classify_openai_exception(exc)

    err = describe_failure(classified, exc, _provider())

    assert isinstance(err, BadRequestError)
    assert err.message == classified.message, "a 4xx's message is the SDK's, unchanged"
    assert is_context_overflow(err)


async def test_a_rejected_request_that_echoes_the_key_has_it_masked_and_nothing_else_changed():
    exc = await _openai_sdk_error(400, {"error": {"message": f"bad header Authorization: Bearer {API_KEY}"}})
    classified = classify_openai_exception(exc)

    err = describe_failure(classified, exc, _provider())

    assert isinstance(err, BadRequestError)
    assert API_KEY not in err.message
    assert err.message == classified.message.replace(API_KEY, "[REDACTED]")


def test_an_exception_the_classifier_could_not_place_is_scrubbed_and_keeps_its_class():
    exc = RuntimeError(f"socket closed while sending {API_KEY}")

    err = describe_failure(classify_openai_exception(exc), exc, _provider())

    assert type(err) is ProviderError
    assert err.message == "socket closed while sending [REDACTED]"


async def test_the_cap_is_applied_after_the_scrub_so_a_secret_is_never_cut_in_half():
    padding = "x" * (UPSTREAM_TEXT_CAP - 8)
    err = _described(await _openai_sdk_error(500, {"error": {"message": f"{padding} {API_KEY}"}}))

    assert API_KEY[:8] not in err.message
    assert "[REDACTED]" in err.message or err.message.endswith("...")


async def test_the_original_exception_is_untouched():
    exc = await _openai_sdk_error(500, {"error": {"message": f"echo {API_KEY}"}})
    before = (str(exc), exc.message, exc.body)

    _described(exc)

    assert (str(exc), exc.message, exc.body) == before
