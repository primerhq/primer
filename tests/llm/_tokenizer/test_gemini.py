"""Gemini count-tokens counter: contents only, bounded, and it raises.

The Developer-API client raises ValueError for system_instruction, tools and
generation_config on a count (verified against google-genai 2.25.0), so this
counter never sends them: it counts the contents natively and estimates the
rest, naming exactly what it estimated.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai import errors as gerrors
from google.genai import models as genai_models
from google.genai import types as genai_types

from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.llm._tokenizer.gemini import COUNT_TIMEOUT_S, count_tokens_gemini_detailed
from primer.llm.counting import NegativeCache, count_prompt_tokens
from primer.model.chat import ImagePart, Message, TextPart, Tool
from primer.model.except_ import (
    BadRequestError,
    NetworkError,
    ProviderError,
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


async def _count(client, messages=USER, tools=None):
    return await count_tokens_gemini_detailed(
        client=client, model="gemini-2.5-pro", messages=messages, tools=tools,
    )


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
        assert [c["role"] for c in kwargs["contents"]] == ["user"]
        estimated = (
            count_tokens_char_fallback(messages=[SYSTEM])
            + count_tokens_char_fallback(messages=[], tools=TOOLS)
        )
        assert got.total == 100 + estimated
        assert got.estimated_components == ("system", "tools")

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

    async def test_a_transport_timeout_is_a_network_error(self) -> None:
        client, _ = _fake_client(exc=httpx.ReadTimeout("slow"))
        with pytest.raises(NetworkError):
            await _count(client)

    async def test_an_unexpected_exception_is_not_swallowed_into_a_number(self) -> None:
        client, _ = _fake_client(exc=RuntimeError("boom"))
        with pytest.raises(ProviderError):
            await _count(client)


class TestThroughTheWrapper:
    class _Llm:
        def __init__(self, client) -> None:
            self.client = client

        async def count_tokens_detailed(self, *, model, messages, tools=None):
            return await count_tokens_gemini_detailed(
                client=self.client, model=model, messages=messages, tools=tools,
            )

    MODEL = SimpleNamespace(provider_id="p", profile_id="prof", model_name="gemini-2.5-pro")

    @pytest.mark.parametrize(
        ("exc", "outcome"),
        [
            (gerrors.ClientError(429, {"error": {"message": "x"}}), "fallback_transient"),
            (gerrors.ServerError(503, {"error": {"message": "x"}}), "fallback_transient"),
            (httpx.ConnectError("down"), "fallback_transient"),
            (gerrors.ClientError(400, {"error": {"message": "x"}}), "fallback_rejected"),
        ],
    )
    async def test_a_failing_client_is_an_estimate_never_native(self, exc, outcome) -> None:
        client, _ = _fake_client(exc=exc)
        result = await count_prompt_tokens(
            self._Llm(client), model=self.MODEL, messages=USER, negative_cache=NegativeCache(),
        )
        assert (result.source, result.outcome) == ("estimate", outcome)

    async def test_system_and_tools_make_the_label_native_plus_estimated(self) -> None:
        client, _ = _fake_client(50)
        result = await count_prompt_tokens(
            self._Llm(client), model=self.MODEL, messages=[SYSTEM, *USER], tools=TOOLS,
            negative_cache=NegativeCache(),
        )
        assert (result.source, result.outcome) == ("native_plus_estimated", "ok")
        assert result.estimated_components == ("system", "tools")
