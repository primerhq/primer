"""Unit tests for the Gemini count-tokens adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.llm._tokenizer.gemini import (
    _CACHE_MAX,
    count_tokens_gemini,
    invalidate_gemini_cache,
)
from primer.model.chat import Message, TextPart, Tool


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    invalidate_gemini_cache()


def _fake_client(token_count: int) -> MagicMock:
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.count_tokens = AsyncMock(
        return_value=MagicMock(total_tokens=token_count)
    )
    return client


class TestCountTokensGemini:
    async def test_returns_api_count(self) -> None:
        client = _fake_client(321)
        msgs = [Message(role="user", parts=[TextPart(text="hi")])]
        n = await count_tokens_gemini(
            client=client, model="gemini-2.5-pro", messages=msgs, tools=None,
        )
        assert n == 321

    async def test_cache_hit_skips_api(self) -> None:
        client = _fake_client(40)
        msgs = [Message(role="user", parts=[TextPart(text="cached")])]
        a = await count_tokens_gemini(
            client=client, model="gemini-2.5-pro", messages=msgs, tools=None,
        )
        b = await count_tokens_gemini(
            client=client, model="gemini-2.5-pro", messages=msgs, tools=None,
        )
        assert a == b == 40
        assert client.aio.models.count_tokens.await_count == 1

    async def test_falls_back_on_api_error(self) -> None:
        client = MagicMock()
        client.aio = MagicMock()
        client.aio.models = MagicMock()
        client.aio.models.count_tokens = AsyncMock(side_effect=RuntimeError("boom"))
        msgs = [Message(role="user", parts=[TextPart(text="hi")])]
        n = await count_tokens_gemini(
            client=client, model="gemini-2.5-pro", messages=msgs, tools=None,
        )
        # Char fallback: 8 + ceil(2/4)=1 = 9.
        assert n == 9

    async def test_cache_max_size(self) -> None:
        assert _CACHE_MAX == 1024
