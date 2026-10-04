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
    """The offline_tiktoken fixture (tests/llm/conftest.py) swaps tiktoken's
    downloaded vocabularies for a byte-level one, so counts here are exact
    and deterministic: one token per UTF-8 byte, plus the module's own
    per-message overhead. They pin primer's logic, not tiktoken's BPE."""

    def test_text_only_gpt4o(self, offline_tiktoken) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="hello world")])]
        n = count_tokens_openai(model="gpt-4o", messages=msgs, tools=None)
        # The payload (role marker + text) is at least the text's own bytes,
        # and the 4-token per-message overhead is on top of it.
        assert n >= len(b"hello world") + 4
        assert offline_tiktoken == ["o200k_base"]

    def test_legacy_gpt4_uses_cl100k(self, offline_tiktoken) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="hello")])]
        n = count_tokens_openai(model="gpt-4", messages=msgs, tools=None)
        assert n > 0
        # The selection is the behaviour under test; "n > 0" alone could not
        # tell cl100k_base from o200k_base.
        assert offline_tiktoken == ["cl100k_base"]

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


class TestCounterContract:
    """The counter never quietly returns a heuristic and never trips on content."""

    def test_content_spelling_a_special_token_is_counted_not_raised(
        self, monkeypatch,
    ) -> None:
        """tiktoken's ``encode`` raises ValueError on '<|endoftext|>' in tool
        output; the counter must use ``encode_ordinary``. Reverting to
        ``encode`` fails this test because the encoding registers the token."""
        import tiktoken

        from primer.llm._tokenizer import _tiktoken_offline

        encoding = tiktoken.Encoding(
            name="with-special",
            pat_str=r"(?s:.)",
            mergeable_ranks={bytes([i]): i for i in range(256)},
            special_tokens={"<|endoftext|>": 256},
        )
        monkeypatch.setattr(
            _tiktoken_offline, "load_encoding", lambda name, **_k: encoding,
        )
        msgs = [Message(role="user", parts=[TextPart(text="page: <|endoftext|> end")])]
        assert count_tokens_openai(model="gpt-4o", messages=msgs) > 0

    def test_an_unavailable_vocabulary_raises_it_does_not_estimate(
        self, monkeypatch,
    ) -> None:
        from primer.llm._tokenizer import _tiktoken_offline
        from primer.model.except_ import TokenCounterUnavailable

        def unavailable(name, **_k):
            raise TokenCounterUnavailable(f"{name}: no vocabulary")

        monkeypatch.setattr(_tiktoken_offline, "load_encoding", unavailable)
        msgs = [Message(role="user", parts=[TextPart(text="hello")])]
        with pytest.raises(TokenCounterUnavailable, match="no vocabulary"):
            count_tokens_openai(model="gpt-4o", messages=msgs)
