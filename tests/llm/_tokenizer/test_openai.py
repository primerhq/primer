"""Unit tests for the tiktoken-backed OpenAI token counter."""

from __future__ import annotations

import pytest

from primer.llm._tokenizer.openai import (
    _MODEL_TO_ENCODING,
    count_tokens_openai,
    resolve_encoding_name,
)
from primer.model.chat import Message, TextPart, Tool


class TestResolveEncoding:
    def test_known_o200k_model(self) -> None:
        assert resolve_encoding_name("gpt-4o") == "o200k_base"
        assert resolve_encoding_name("gpt-4o-mini") == "o200k_base"
        assert resolve_encoding_name("o1") == "o200k_base"
        assert resolve_encoding_name("o3-mini") == "o200k_base"
        assert resolve_encoding_name("o4-mini") == "o200k_base"

    def test_known_cl100k_model(self) -> None:
        assert resolve_encoding_name("gpt-4") == "cl100k_base"
        assert resolve_encoding_name("gpt-4-turbo") == "cl100k_base"
        assert resolve_encoding_name("gpt-3.5-turbo") == "cl100k_base"

    def test_provider_prefix_stripped(self) -> None:
        assert resolve_encoding_name("openai/gpt-4o") == "o200k_base"

    def test_unknown_defaults_to_o200k(self) -> None:
        assert resolve_encoding_name("future-mystery-model") == "o200k_base"


class TestCountTokensOpenAI:
    def test_text_only_gpt4o(self) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="hello world")])]
        n = count_tokens_openai(model="gpt-4o", messages=msgs, tools=None)
        assert 2 <= n <= 20

    def test_legacy_gpt4_uses_cl100k(self) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="hello")])]
        n = count_tokens_openai(model="gpt-4", messages=msgs, tools=None)
        assert n > 0

    def test_tools_increase_count(self) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="x")])]
        base = count_tokens_openai(model="gpt-4o", messages=msgs, tools=None)
        with_tools = count_tokens_openai(
            model="gpt-4o",
            messages=msgs,
            tools=[
                Tool(
                    id="ls",
                    description="list",
                    toolset_id="x",
                    args_schema={"type": "object", "properties": {}},
                )
            ],
        )
        assert with_tools > base

    def test_caching_does_not_change_result(self) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="cached")])]
        a = count_tokens_openai(model="gpt-4o", messages=msgs, tools=None)
        b = count_tokens_openai(model="gpt-4o", messages=msgs, tools=None)
        assert a == b
