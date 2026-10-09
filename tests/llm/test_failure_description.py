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
    """The key sits ACROSS where a cap-first order would cut (the text is 5 characters longer than the cap before the scrub and 9 shorter after
    it): cut first, 16 of its characters are left readable and no exact match can find them; scrubbed first, the whole key is one mask."""
    padding = "x" * (UPSTREAM_TEXT_CAP - 3 - 16 - 1)
    err = _described(await _openai_sdk_error(500, {"error": {"message": f"{padding} {API_KEY}"}}))

    assert not any(API_KEY[i:i + 8] in err.message for i in range(len(API_KEY) - 7)), err.message
    assert err.message.endswith(" [REDACTED]"), err.message


async def test_the_original_exception_is_untouched():
    exc = await _openai_sdk_error(500, {"error": {"message": f"echo {API_KEY}"}})
    before = (str(exc), exc.message, exc.body)

    _described(exc)

    assert (str(exc), exc.message, exc.body) == before


def _hostile_body(depth: int) -> openai.InternalServerError:
    body: dict = {"code": "x"}
    for _ in range(depth):
        body = {"error": body}
    exc = openai.InternalServerError.__new__(openai.InternalServerError)
    exc.status_code, exc.code, exc.message, exc.body = 500, None, "Error code: 500", body
    Exception.__init__(exc, exc.message)
    return exc


def test_a_body_that_nests_to_the_recursion_limit_is_read_to_a_bounded_depth():
    err = _described(_hostile_body(5000))

    assert isinstance(err, ServerError) and err.code == "server_error"
    assert err.message.startswith(f"{LABEL} had a server error (HTTP 500)") and len(err.message) <= len(LABEL) + 60 + UPSTREAM_TEXT_CAP


def test_a_message_found_a_few_levels_down_is_still_found():
    body = {"error": {"error": {"error": {"message": "three levels down"}}}}
    exc = openai.InternalServerError.__new__(openai.InternalServerError)
    exc.status_code, exc.code, exc.message, exc.body = 500, None, "Error code: 500", body
    Exception.__init__(exc, exc.message)

    assert _described(exc).message.endswith("(HTTP 500): three levels down")


def test_a_body_nobody_can_read_leaves_the_sentence_without_its_text_instead_of_crashing(monkeypatch):
    import primer.llm._failure as failure

    def unreadable(exc, err):
        raise RuntimeError("cannot read this body")

    monkeypatch.setattr(failure, "_upstream_text", unreadable)
    exc = _hostile_body(3)

    assert _described(exc).message == f"{LABEL} had a server error (HTTP 500)"


async def test_control_characters_are_stripped_before_the_text_is_capped():
    err = _described(await _openai_sdk_error(500, {"error": {"message": "bad\x00 gate\x1b[31mway\ttab"}}))

    assert "\x00" not in err.message and "\x1b" not in err.message
    assert err.message.endswith("(HTTP 500): bad gate[31mway tab")


async def test_an_untouched_message_is_capped_generously_after_the_scrub():
    """A 400 keeps the SDK's message (the overflow veto reads it), but a multi-megabyte body must not land whole in the session record."""
    exc = await _openai_sdk_error(400, {"error": {"message": "x" * 200_000}})

    err = _described(exc)

    assert isinstance(err, BadRequestError) and 3000 < len(err.message) <= 4000 and err.message.endswith("...")


def test_the_bearer_token_rule_is_pinned():
    exc = RuntimeError("rejected: Authorization: Bearer abcdef0123456789ABCDEF.token_part")

    err = describe_failure(classify_openai_exception(exc), exc, _provider())

    assert "abcdef0123456789" not in err.message and "Bearer [REDACTED]" in err.message


def test_the_url_borne_key_rule_is_pinned():
    exc = RuntimeError("GET https://host/v1/models?key=SECRETVALUE123456&alt=json failed")

    err = describe_failure(classify_openai_exception(exc), exc, _provider())

    assert "SECRETVALUE123456" not in err.message and "key=[REDACTED]&alt=json" in err.message


def test_a_percent_escaped_base_url_password_is_masked_in_both_spellings():
    provider = _provider("http://user:p%40ss%2Fword99@lmstudio.local:1234/v1")
    exc = RuntimeError("tried p%40ss%2Fword99 and then p@ss/word99 against the server")

    err = describe_failure(classify_openai_exception(exc), exc, provider)

    assert "word99" not in err.message and err.message.count("[REDACTED]") == 2, err.message


def test_ollama_network_failures_carry_their_reason():
    import ollama

    from primer.llm.ollama import _classify_ollama_exception

    refused = ConnectionError("Failed to connect to Ollama at http://user:hunter2@lmstudio.local:1234; is it running?")
    request_error = ollama.RequestError("model name is required")
    for exc, wants in ((refused, "Failed to connect to Ollama"), (request_error, "model name is required")):
        err = describe_failure(_classify_ollama_exception(exc), exc, _provider("http://user:hunter2@lmstudio.local:1234/v1"))

        assert isinstance(err, NetworkError) and err.code == "network_error", (type(exc).__name__, err)
        assert err.message.startswith(f"{LABEL} could not be reached") and wants in err.message, err.message
        assert "hunter2" not in err.message


def test_an_ollama_error_that_is_a_decoded_json_object_keeps_its_text():
    import ollama

    from primer.llm.ollama import _classify_ollama_exception

    exc = ollama.ResponseError("placeholder", 500)
    exc.error = {"error": {"message": "the model crashed"}}

    err = describe_failure(_classify_ollama_exception(exc), exc, _provider())

    assert isinstance(err, ServerError) and err.message.endswith("(HTTP 500): the model crashed"), err.message


# ---- round 2 of the review of PR 619: the key floors, Basic auth, the walk bounds, control characters, the fallback log ----------------------------

KEYLESS_PLACEHOLDERS = ("EMPTY", "none", "dummy", "ollama", "lm-studio", "no-key-required", "not-needed", "sk-no-key-required")


def _provider_with_key(key: str, url: str = "http://lmstudio.local:1234/v1") -> LLMProvider:
    return LLMProvider(
        id="lm-studio-box", provider=LLMProviderType.OPENCHAT, limits=Limits(max_concurrency=1),
        config=OpenChatConfig(url=HttpUrl(url), api_key=SecretStr(key), flavor=OpenChatFlavor.LMSTUDIO),
    )


@pytest.mark.parametrize("placeholder", KEYLESS_PLACEHOLDERS + ("empty", "None", "Dummy", "OLLAMA"))
def test_a_keyless_placeholder_is_never_masked_whatever_its_length_or_case(placeholder):
    """A keyless local endpoint is configured with ``EMPTY``, ``dummy``, ``lm-studio``...: masking it would blank those words (and numbers) everywhere."""
    exc = RuntimeError(f"the {placeholder} model returned none of the 1234 results")

    err = describe_failure(classify_openai_exception(exc), exc, _provider_with_key(placeholder))

    assert err.message == f"the {placeholder} model returned none of the 1234 results", err.message


@pytest.mark.parametrize("key", ["abcd", "sk-1234", "secret1", "abc12345", "token-abc123"])
async def test_a_configured_key_of_four_or_more_characters_is_masked_where_a_provider_echoes_it(key):
    """The 8/6 floors of the first revision let ``sk-1234``, ``secret1`` and a short password through verbatim in a 500, a 401 and a 400 echo."""
    provider = _provider_with_key(key)
    in_500 = _described(await _openai_sdk_error(500, {"error": {"message": f"invalid api key {key} for this server"}}), provider)
    in_401 = _described(await _openai_sdk_error(401, {"error": {"message": f"Malformed API Key passed in: Bearer {key}"}}), provider)
    in_400 = _described(await _openai_sdk_error(400, {"error": {"message": f"key {key} may not call this model"}}), provider)

    for err in (in_500, in_401, in_400):
        assert key not in err.message and "[REDACTED]" in err.message, err.message


def test_the_floor_is_four_characters_and_a_short_key_is_masked_as_a_token_not_inside_a_longer_one():
    """4 to 7 characters: masked where the key stands alone (a number that merely contains it is not a key: the context-overflow veto reads those),
    8 and more: masked wherever it appears. Below 4 nothing is masked."""
    def masked(key: str, text: str) -> str:
        exc = RuntimeError(text)
        return describe_failure(classify_openai_exception(exc), exc, _provider_with_key(key)).message

    assert masked("1234", "you requested 12345678 tokens, not 1234") == "you requested 12345678 tokens, not [REDACTED]"
    assert masked("secret1", "key secret1x and secret1.") == "key secret1x and [REDACTED]."
    assert masked("abc12345", "key xabc12345y") == "key x[REDACTED]y", "8 characters: masked wherever it appears"
    assert masked("abc", "key abc") == "key abc", "below the floor nothing is masked"


async def test_a_short_base_url_password_is_masked_in_both_forms():
    for password in ("pw12", "hunt3"):
        provider = _provider(f"http://admin:{password}@lmstudio.local:1234/v1")
        bare = _described(await _openai_sdk_error(500, {"error": {"message": f"login admin/{password} refused"}}), provider)
        in_url = _described(await _openai_sdk_error(502, {"error": {"message": f"proxy http://admin:{password}@lmstudio.local:1234/v1 refused"}}), provider)

        assert password not in bare.message and password not in in_url.message, (bare.message, in_url.message)


async def test_the_basic_authorization_header_of_a_base_url_with_credentials_is_masked_when_a_provider_echoes_it():
    """httpx sends ``Authorization: Basic base64(user:password)`` for a Base URL with userinfo, so a 401 that echoes the header shows the password."""
    import base64

    provider = _provider("http://admin:Tr0ub4dor-pw@lmstudio.local:1234/v1")
    header = base64.b64encode(b"admin:Tr0ub4dor-pw").decode()
    exact = _described(await _openai_sdk_error(401, {"error": {"message": f"bad header Authorization: Basic {header}"}}), provider)
    other = _described(await _openai_sdk_error(401, {"error": {"message": "bad header Authorization: Basic YWRtaW46c29tZXRoaW5nZWxzZQ=="}}), provider)

    assert header not in exact.message and "Tr0ub4dor" not in exact.message and "Basic [REDACTED]" in exact.message, exact.message
    assert "YWRtaW46" not in other.message and "Basic [REDACTED]" in other.message, other.message


def test_an_ordinary_sentence_that_says_basic_is_not_masked():
    """Only a run that looks like base64 follows ``Basic`` into the mask; the word after it in prose does not."""
    exc = RuntimeError("Basic authentication is required for this realm")

    err = describe_failure(classify_openai_exception(exc), exc, _provider())

    assert err.message == "Basic authentication is required for this realm"


def test_a_body_nested_far_past_the_walk_ends_with_the_bounded_dump_and_logs_nothing(caplog):
    """Reverting the walk to recursion survives a test that only checks the sentence: the RecursionError is swallowed and the label-only sentence
    passes. The text must END with the four-level dump of what the walk reached, and nothing may be logged by the module."""
    with caplog.at_level("DEBUG", logger="primer.llm._failure"):
        err = _described(_hostile_body(5000))

    assert err.message.endswith(': {"error": {"error": {"error": {"error": "..."}}}}'), err.message[-120:]
    assert [r for r in caplog.records if r.name == "primer.llm._failure"] == []


async def test_a_control_character_inside_an_echoed_key_does_not_defeat_the_mask():
    """Stripped before the scrub, a NUL inside the echo no longer breaks the exact match."""
    echoed = API_KEY[:10] + "\x00" + API_KEY[10:]
    err = _described(await _openai_sdk_error(500, {"error": {"message": f"bad key {echoed}"}}))

    assert not any(API_KEY[i:i + 8] in err.message for i in range(len(API_KEY) - 7)), err.message
    assert "[REDACTED]" in err.message


async def test_an_untouched_message_has_its_control_characters_stripped_too():
    err = _described(await _openai_sdk_error(400, {"error": {"message": "bad\x00 value\x1b[31m here"}}))

    assert isinstance(err, BadRequestError) and "\x00" not in err.message and "\x1b" not in err.message


def test_the_fallback_log_does_not_carry_the_exception_that_failed_or_the_one_it_chains_to(monkeypatch, caplog):
    """The fallback is reached inside the adapter's ``except``, so a traceback logged there chains the SDK exception whose text is the raw body (and
    whatever key it echoes). Only the type's name is logged."""
    from primer.llm import _failure

    def boom(exc, err):
        raise RuntimeError(f"cannot read {API_KEY}")

    monkeypatch.setattr(_failure, "_upstream_text", boom)
    exc = RuntimeError(f"echo {API_KEY}")

    with caplog.at_level("WARNING", logger="primer.llm._failure"):
        try:
            raise exc
        except RuntimeError as caught:
            err = describe_failure(ServerError("x", code="server_error", status_code=500), caught, _provider())

    assert err.message.endswith("(HTTP 500)")
    assert caplog.records and API_KEY not in caplog.text and "RuntimeError" in caplog.text


async def test_only_a_prefix_of_a_huge_body_is_scrubbed(monkeypatch):
    """The scrub is linear in what it is handed and a body can be tens of megabytes; the message shows 300 characters of it."""
    from primer.llm import _failure

    seen: list[int] = []
    real = _failure.scrub

    def spy(text, provider):
        seen.append(len(text))
        return real(text, provider)

    monkeypatch.setattr(_failure, "scrub", spy)

    err = _described(await _openai_sdk_error(502, "<p>" + "x" * 3_000_000))

    assert seen and max(seen) <= _failure._SCAN_LIMIT, seen
    assert err.message.startswith(f"{LABEL} had a server error (HTTP 502): <p>xxx")


def test_an_ollama_connection_error_raised_from_none_carries_its_own_text_not_the_suppressed_one():
    """ollama raises ``ConnectionError("Failed to connect to Ollama. ...") from None`` inside ``except httpx.ConnectError``. ``from None`` suppresses the
    context on purpose: the useful sentence is the ConnectionError's own, not the transport's "All connection attempts failed"."""
    from primer.llm.ollama import _classify_ollama_exception

    try:
        try:
            raise httpx.ConnectError("All connection attempts failed")
        except httpx.ConnectError:
            raise ConnectionError("Failed to connect to Ollama. Please check that Ollama is running.") from None
    except ConnectionError as exc:
        err = describe_failure(_classify_ollama_exception(exc), exc, _provider())

    assert isinstance(err, NetworkError)
    assert err.message.endswith("(ConnectionError): Failed to connect to Ollama. Please check that Ollama is running."), err.message
