"""Unit tests for the HF-tokenizer counter (Ollama adapter)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from primer.llm._tokenizer.hf import (
    _TOKENIZER_CACHE,
    count_tokens_hf,
    invalidate_hf_cache,
)
from primer.model.chat import Message, TextPart


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    invalidate_hf_cache()


class TestCountTokensHF:
    def test_uses_cached_tokenizer(self) -> None:
        fake_tok = MagicMock()
        fake_tok.encode.return_value = [1, 2, 3, 4, 5]

        with patch(
            "primer.llm._tokenizer.hf.AutoTokenizer"
        ) as mock_auto:
            mock_auto.from_pretrained.return_value = fake_tok
            msgs = [Message(role="user", parts=[TextPart(text="hello")])]
            n1 = count_tokens_hf(model="llama3.2", messages=msgs, tools=None)
            n2 = count_tokens_hf(model="llama3.2", messages=msgs, tools=None)
            assert n1 == n2 == 5
            assert mock_auto.from_pretrained.call_count == 1

    def test_falls_back_on_load_failure(self) -> None:
        with patch(
            "primer.llm._tokenizer.hf.AutoTokenizer"
        ) as mock_auto:
            mock_auto.from_pretrained.side_effect = OSError("not on hub")
            msgs = [Message(role="user", parts=[TextPart(text="hello")])]
            n = count_tokens_hf(model="unknown-model", messages=msgs, tools=None)
            # Char fallback: 8 + ceil(5/4)=2 = 10
            assert n == 10

    def test_different_models_use_different_tokenizers(self) -> None:
        fake_a = MagicMock(); fake_a.encode.return_value = [1] * 3
        fake_b = MagicMock(); fake_b.encode.return_value = [1] * 7
        with patch("primer.llm._tokenizer.hf.AutoTokenizer") as mock_auto:
            mock_auto.from_pretrained.side_effect = [fake_a, fake_b]
            msgs = [Message(role="user", parts=[TextPart(text="x")])]
            na = count_tokens_hf(model="llama3.2", messages=msgs, tools=None)
            nb = count_tokens_hf(model="qwen2.5", messages=msgs, tools=None)
            assert na == 3
            assert nb == 7
