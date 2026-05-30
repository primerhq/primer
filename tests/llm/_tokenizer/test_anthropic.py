"""Unit tests for the Anthropic count-tokens adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.llm._tokenizer.anthropic import (
    _CACHE_MAX,
    count_tokens_anthropic,
    invalidate_anthropic_cache,
)
from primer.model.chat import Message, TextPart, Tool


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    invalidate_anthropic_cache()


def _fake_client(token_count: int) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.count_tokens = AsyncMock(
        return_value=MagicMock(input_tokens=token_count)
    )
    return client


class TestCountTokensAnthropic:
    async def test_returns_api_count(self) -> None:
        client = _fake_client(123)
        msgs = [Message(role="user", parts=[TextPart(text="hi")])]
        n = await count_tokens_anthropic(
            client=client, model="claude-opus-4-7", messages=msgs, tools=None,
        )
        assert n == 123
        client.messages.count_tokens.assert_awaited_once()

    async def test_cache_hit_skips_api(self) -> None:
        client = _fake_client(50)
        msgs = [Message(role="user", parts=[TextPart(text="cached")])]
        a = await count_tokens_anthropic(
            client=client, model="claude-opus-4-7", messages=msgs, tools=None,
        )
        b = await count_tokens_anthropic(
            client=client, model="claude-opus-4-7", messages=msgs, tools=None,
        )
        assert a == b == 50
        assert client.messages.count_tokens.await_count == 1

    async def test_cache_keyed_by_model(self) -> None:
        client = _fake_client(77)
        msgs = [Message(role="user", parts=[TextPart(text="hi")])]
        await count_tokens_anthropic(
            client=client, model="claude-opus-4-7", messages=msgs, tools=None,
        )
        await count_tokens_anthropic(
            client=client, model="claude-haiku-4-5-20251001", messages=msgs, tools=None,
        )
        assert client.messages.count_tokens.await_count == 2

    async def test_falls_back_on_api_error(self) -> None:
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.count_tokens = AsyncMock(side_effect=RuntimeError("boom"))
        msgs = [Message(role="user", parts=[TextPart(text="hello")])]
        n = await count_tokens_anthropic(
            client=client, model="claude-opus-4-7", messages=msgs, tools=None,
        )
        # Char-fallback floor: 8 + ceil(5/4)=2 = 10.
        assert n == 10

    async def test_tools_included_in_request(self) -> None:
        client = _fake_client(99)
        msgs = [Message(role="user", parts=[TextPart(text="x")])]
        tools = [
            Tool(
                id="ls", description="list", toolset_id="x",
                args_schema={"type": "object", "properties": {}},
            )
        ]
        await count_tokens_anthropic(
            client=client, model="claude-opus-4-7", messages=msgs, tools=tools,
        )
        call_kwargs = client.messages.count_tokens.await_args.kwargs
        assert call_kwargs["model"] == "claude-opus-4-7"
        assert call_kwargs["tools"]
        assert call_kwargs["tools"][0]["name"] == "ls"

    async def test_cache_max_size(self) -> None:
        assert _CACHE_MAX == 1024
