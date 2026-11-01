"""LLM retries (Part 2 T50, AG-H5).

The SDK clients kept their own retries (the SDK default of 2) under ``RetryingLLM``, so ``max_retries: 0`` still sent a 429
three times and the default of 2 sent it nine times, with the SDK's backoff inside the concurrency slot. The SDK no longer
retries; the wrapper's pause honours the provider's ``retry-after`` instead, capped by ``retry_backoff_max_seconds``.
"""

from __future__ import annotations

import openai
import pytest

from primer.llm._retry import stream_with_retry
from primer.llm.retrying import RetryingLLM
from primer.model.chat import Done
from primer.model.except_ import RateLimitError
from tests.llm.test_adapter_characterization import _ADAPTERS, _collect, _provider, _route, _sdk_http

_SDK_RETRYING = ["openchat", "openrouter", "openresponses", "anthropic"]
_429 = (429, b'{"error": {"message": "slow down"}}', {"retry-after-ms": "1", "retry-after": "0"})


class TestTheSdkDoesNotRetry:
    @pytest.mark.parametrize("kind", _SDK_RETRYING)
    @pytest.mark.parametrize(("max_retries", "hits"), [(0, 1), (2, 3)])
    async def test_a_429_is_sent_max_retries_plus_one_times(self, monkeypatch, kind, max_retries, hits) -> None:
        wire = _route(monkeypatch, kind, [_429])
        prov = _provider(kind, max_retries=max_retries, retry_backoff_seconds=0.0, retry_backoff_max_seconds=0.0)
        with pytest.raises(RateLimitError):
            await _collect(RetryingLLM(_ADAPTERS[kind](prov), prov), kind)
        assert len(wire.requests) == hits

    @pytest.mark.parametrize("kind", _SDK_RETRYING)
    async def test_the_client_is_built_with_no_retries(self, kind) -> None:
        llm = _ADAPTERS[kind](_provider(kind))
        try:
            assert llm._get_client().max_retries == 0
        finally:
            await llm.aclose()


def _rate_limited(headers: dict[str, str]) -> RateLimitError:
    """What an adapter raises for a 429: the SDK exception, with its response, is the ``cause``."""
    http = _sdk_http(openai.AsyncOpenAI(api_key="probe"))
    response = http.Response(429, headers=headers, request=http.Request("POST", "http://x/v1/chat/completions"))
    return RateLimitError("rate limited", code="rate_limit", cause=openai.RateLimitError("slow", response=response, body=None))


class TestRetryAfter:
    async def _pauses(self, cause: Exception, *, cap: float) -> list[float]:
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        attempts = {"n": 0}

        def open_stream():
            attempts["n"] += 1

            async def gen():
                if attempts["n"] == 1:
                    raise cause
                yield Done(stop_reason="stop", raw_reason="stop")
            return gen()

        events = [e async for e in stream_with_retry(
            open_stream, provider_id="p", provider_kind="openchat", max_retries=2,
            base_backoff_seconds=0.0, max_backoff_seconds=cap, sleep=sleep,
        )]
        assert len(events) == 1
        return slept

    @pytest.mark.parametrize(("headers", "cap", "expected"), [
        ({"retry-after": "3"}, 8.0, 3.0),
        ({"retry-after": "30"}, 8.0, 8.0),
        ({"retry-after-ms": "1500"}, 8.0, 1.5),
        ({"retry-after-ms": "250", "retry-after": "9"}, 8.0, 0.25),
    ])
    async def test_the_pause_is_the_providers_retry_after_capped(self, headers, cap, expected) -> None:
        assert await self._pauses(_rate_limited(headers), cap=cap) == [expected]

    @pytest.mark.parametrize("headers", [{}, {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, {"retry-after": "-4"}])
    async def test_without_a_usable_header_the_jittered_backoff_applies(self, headers) -> None:
        # base 0: the jittered backoff is exactly 0.
        assert await self._pauses(_rate_limited(headers), cap=8.0) == [0.0]
