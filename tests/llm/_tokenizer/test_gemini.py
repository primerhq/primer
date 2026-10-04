"""Gemini count-tokens counter: contents only, bounded, and it raises.

The Developer-API client raises ValueError for system_instruction, tools and
generation_config on a count (verified against google-genai 2.25.0), so this
counter never sends them: it counts the contents natively and estimates the
rest, naming exactly what it estimated.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import httpx
import pytest
from aiohttp.client_reqrep import ConnectionKey
from google.genai import errors as gerrors
from google.genai import models as genai_models
from google.genai import types as genai_types

from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.llm._tokenizer.gemini import COUNT_TIMEOUT_S, count_tokens_gemini_detailed
from primer.llm.counting import NegativeCache, count_prompt_tokens
from primer.llm.gemini import _messages_to_gemini
from primer.model.chat import ImagePart, Message, TextPart, Tool, ToolCallPart, ToolResultPart
from primer.model.except_ import (
    BadRequestError,
    NetworkError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    TokenCounterUnavailable,
)
from primer.model.media_tokens import IMAGE_TOKENS

USER = [Message(role="user", parts=[TextPart(text="hi")])]
SYSTEM = Message(role="system", parts=[TextPart(text="be brief")])
TOOLS = [Tool(id="ls", description="list", toolset_id="x",
              args_schema={"type": "object", "properties": {}})]


def _fake_client(tokens: int = 0, exc: Exception | None = None):
    count = AsyncMock(return_value=SimpleNamespace(total_tokens=tokens), side_effect=exc)
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(count_tokens=count))), count


async def _count(client, messages=USER, tools=None, **kwargs):
    return await count_tokens_gemini_detailed(
        client=client, model="gemini-2.5-pro", messages=messages, tools=tools,
        messages_to_contents=_messages_to_gemini, **kwargs,
    )


def _sleeping_client(seconds: float):
    """A client whose count outlasts its own per-attempt timeout, the way
    google-genai 2.25's aiohttp path does after a connection error: it sleeps
    ``1 + randint(0, 9)`` seconds and then retries once (``_api_client.py``), so the
    per-call ``http_options.timeout`` alone does not bound the call."""
    calls = {"n": 0}

    async def count_tokens(**_kwargs):
        calls["n"] += 1
        await asyncio.sleep(seconds)
        return SimpleNamespace(total_tokens=1)

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(count_tokens=count_tokens)))
    return client, calls


def _connector_error() -> aiohttp.ClientConnectorError:
    key = ConnectionKey("generativelanguage.googleapis.com", 443, True, None, None, None, None)
    return aiohttp.ClientConnectorError(key, OSError(111, "Connection refused"))


class TestRequestShape:
    async def test_returns_the_endpoints_count_and_says_it_is_exact(self) -> None:
        client, _ = _fake_client(321)
        got = await _count(client)
        assert (got.total, got.exact, got.estimated_components) == (321, True, ())

    async def test_the_call_is_bounded_by_a_per_call_timeout(self) -> None:
        client, count = _fake_client(1)
        await _count(client)
        assert count.await_args.kwargs["config"] == {
            "http_options": {"timeout": int(COUNT_TIMEOUT_S * 1000)},
        }

    async def test_system_and_tools_are_never_sent_only_estimated(self) -> None:
        client, count = _fake_client(100)
        got = await _count(client, [SYSTEM, *USER], tools=TOOLS)
        kwargs = count.await_args.kwargs
        assert set(kwargs) == {"model", "contents", "config"}
        assert set(kwargs["config"]) == {"http_options"}
        for forbidden in ("system_instruction", "tools", "generation_config"):
            assert forbidden not in kwargs["config"]
        assert [c.role for c in kwargs["contents"]] == ["user"]
        estimated = (
            count_tokens_char_fallback(messages=[SYSTEM])
            + count_tokens_char_fallback(messages=[], tools=TOOLS)
        )
        assert got.total == 100 + estimated
        assert got.estimated_components == ("system", "tools")

    async def test_a_tool_using_history_is_sent_as_user_and_model_contents(self) -> None:
        """The count API takes only user and model; the live translator makes tool
        results user-role function_response parts. A private copy once sent role
        'tool', which is rejected."""
        client, count = _fake_client(1)
        await _count(client, [
            *USER,
            Message(role="assistant", parts=[ToolCallPart(id="t1", name="ls", arguments={})]),
            Message(role="tool", parts=[ToolResultPart(id="t1", output="a b c")]),
        ])
        contents = count.await_args.kwargs["contents"]
        assert [c.role for c in contents] == ["user", "model", "user"]
        assert contents[2].parts[0].function_response.name == "ls"

    async def test_the_contents_are_exactly_what_the_live_translator_builds(self) -> None:
        history = [SYSTEM, *USER,
                   Message(role="assistant", parts=[ToolCallPart(id="t1", name="ls", arguments={})]),
                   Message(role="tool", parts=[ToolResultPart(id="t1", output="x")])]
        client, count = _fake_client(1)
        await _count(client, history)
        assert count.await_args.kwargs["contents"] == _messages_to_gemini(history)[1]

    async def test_the_request_is_accepted_by_the_real_sdk_converter(self) -> None:
        """The Developer-API conversion raises ValueError on the three forbidden
        fields; what we send must pass it."""
        client, count = _fake_client(1)
        await _count(client, [SYSTEM, *USER], tools=TOOLS)
        config = genai_types.CountTokensConfig.model_validate(count.await_args.kwargs["config"])
        genai_models._CountTokensConfig_to_mldev(config.model_dump(exclude_none=True), {})
        with pytest.raises(ValueError, match="only supported in Gemini Enterprise"):
            genai_models._CountTokensConfig_to_mldev({"system_instruction": "x"}, {})

    async def test_media_is_an_estimated_component(self) -> None:
        client, _ = _fake_client(10)
        got = await _count(client, [Message(role="user", parts=[
            TextPart(text="look"), ImagePart(mime_type="image/png", data=b"\x00"),
        ])])
        assert got.total == 10 + IMAGE_TOKENS
        assert got.estimated_components == ("media",)

    async def test_no_contents_is_unavailable_without_a_call(self) -> None:
        client, count = _fake_client(1)
        with pytest.raises(TokenCounterUnavailable, match="no natively countable content"):
            await _count(client, [SYSTEM])
        count.assert_not_awaited()


class TestFailuresRaiseMappedErrors:
    async def test_a_429_is_a_rate_limit(self) -> None:
        exc = gerrors.ClientError(429, {"error": {"message": "slow down"}})
        client, _ = _fake_client(exc=exc)
        with pytest.raises(RateLimitError):
            await _count(client)

    async def test_a_400_is_a_bad_request(self) -> None:
        exc = gerrors.ClientError(400, {"error": {"message": "bad"}})
        client, _ = _fake_client(exc=exc)
        with pytest.raises(BadRequestError):
            await _count(client)

    @pytest.mark.parametrize(
        "exc", [TimeoutError(), httpx.ReadTimeout("slow"), aiohttp.ServerTimeoutError("slow")],
        ids=["builtin", "httpx", "aiohttp-server-timeout"],
    )
    async def test_a_timeout_is_a_provider_timeout(self, exc) -> None:
        """google-genai 2.x calls through aiohttp, so a stalled count raises the
        builtin TimeoutError; the httpx-only classifier left it a bare ProviderError."""
        client, _ = _fake_client(exc=exc)
        with pytest.raises(ProviderTimeoutError):
            await _count(client)

    async def test_a_count_that_outlives_the_deadline_is_a_timeout_not_a_long_wait(self) -> None:
        """The documented bound must be real: a count stuck in the SDK's sleep and
        retry is cut off at ``timeout_s`` and reported as a timeout."""
        client, calls = _sleeping_client(3.0)
        started = time.monotonic()
        with pytest.raises(ProviderTimeoutError):
            await _count(client, timeout_s=0.1)
        assert time.monotonic() - started < 1.0, "the call waited out the SDK's sleep"
        assert calls["n"] == 1

    async def test_a_408_is_a_timeout_and_a_425_is_retry_later_not_a_rejection(self) -> None:
        """Both are 4xx with no SDK class; promoted to a rejection they would never
        be negative-cached and every count would hit the endpoint again."""
        for status, expected in ((408, ProviderTimeoutError), (425, RateLimitError)):
            client, _ = _fake_client(exc=gerrors.ClientError(status, {"error": {"message": "x"}}))
            with pytest.raises(expected) as caught:
                await _count(client)
            assert caught.value.status_code == status

    async def test_an_unreachable_host_is_a_network_error(self) -> None:
        client, _ = _fake_client(exc=_connector_error())
        with pytest.raises(NetworkError):
            await _count(client)

    async def test_any_aiohttp_client_error_is_a_network_error(self) -> None:
        client, _ = _fake_client(exc=aiohttp.ServerDisconnectedError())
        with pytest.raises(NetworkError):
            await _count(client)

    async def test_an_unexpected_exception_is_not_swallowed_into_a_number(self) -> None:
        client, _ = _fake_client(exc=RuntimeError("boom"))
        with pytest.raises(ProviderError):
            await _count(client)


class TestThroughTheWrapper:
    class _Llm:
        def __init__(self, client, timeout_s: float = COUNT_TIMEOUT_S) -> None:
            self.client = client
            self.timeout_s = timeout_s

        async def count_tokens_detailed(self, *, model, messages, tools=None):
            return await count_tokens_gemini_detailed(
                client=self.client, model=model, messages=messages, tools=tools,
                messages_to_contents=_messages_to_gemini, timeout_s=self.timeout_s,
            )

    MODEL = SimpleNamespace(provider_id="p", profile_id="prof", model_name="gemini-2.5-pro")

    @pytest.mark.parametrize(
        ("exc", "outcome"),
        [
            (gerrors.ClientError(429, {"error": {"message": "x"}}), "fallback_transient"),
            (gerrors.ServerError(503, {"error": {"message": "x"}}), "fallback_transient"),
            (httpx.ConnectError("down"), "fallback_transient"),
            (TimeoutError(), "fallback_timeout"),
            (aiohttp.ServerTimeoutError("slow"), "fallback_timeout"),
            (_connector_error(), "fallback_transient"),
            (gerrors.ClientError(400, {"error": {"message": "x"}}), "fallback_rejected"),
            (gerrors.ClientError(408, {"error": {"message": "x"}}), "fallback_timeout"),
            (gerrors.ClientError(425, {"error": {"message": "x"}}), "fallback_transient"),
        ],
    )
    async def test_a_failing_client_is_an_estimate_never_native(self, exc, outcome) -> None:
        client, _ = _fake_client(exc=exc)
        result = await count_prompt_tokens(
            self._Llm(client), model=self.MODEL, messages=USER, negative_cache=NegativeCache(),
        )
        assert (result.source, result.outcome) == ("estimate", outcome)

    async def test_a_stalled_count_is_negative_cached_so_it_does_not_re_stall_every_turn(self) -> None:
        client, count = _fake_client(exc=TimeoutError())
        cache = NegativeCache()
        llm = self._Llm(client)
        first = await count_prompt_tokens(llm, model=self.MODEL, messages=USER, negative_cache=cache)
        second = await count_prompt_tokens(llm, model=self.MODEL, messages=USER, negative_cache=cache)
        assert (first.outcome, second.outcome) == ("fallback_timeout", "negative_cached")
        assert count.await_count == 1, "the second turn must not wait out the timeout again"

    async def test_a_count_stuck_in_the_sdk_retry_sleep_is_a_cached_timeout(self) -> None:
        """Through the wrapper: the deadline turns the SDK's sleep-and-retry into a
        labelled fallback_timeout, and the next turn does not wait again."""
        client, calls = _sleeping_client(3.0)
        cache = NegativeCache()
        llm = self._Llm(client, timeout_s=0.1)
        started = time.monotonic()
        first = await count_prompt_tokens(llm, model=self.MODEL, messages=USER, negative_cache=cache)
        second = await count_prompt_tokens(llm, model=self.MODEL, messages=USER, negative_cache=cache)
        assert (first.outcome, second.outcome) == ("fallback_timeout", "negative_cached")
        assert calls["n"] == 1
        assert time.monotonic() - started < 1.0

    async def test_an_aiohttp_connection_failure_is_negative_cached_too(self) -> None:
        client, count = _fake_client(exc=_connector_error())
        cache = NegativeCache()
        llm = self._Llm(client)
        await count_prompt_tokens(llm, model=self.MODEL, messages=USER, negative_cache=cache)
        again = await count_prompt_tokens(llm, model=self.MODEL, messages=USER, negative_cache=cache)
        assert again.outcome == "negative_cached" and count.await_count == 1

    async def test_system_and_tools_make_the_label_native_plus_estimated(self) -> None:
        client, _ = _fake_client(50)
        result = await count_prompt_tokens(
            self._Llm(client), model=self.MODEL, messages=[SYSTEM, *USER], tools=TOOLS,
            negative_cache=NegativeCache(),
        )
        assert (result.source, result.outcome) == ("native_plus_estimated", "ok")
        assert result.estimated_components == ("system", "tools")
