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
    # transformers is imported lazily inside _get_tokenizer (it lives in
    # the optional 'huggingface' extra), so the patch target is the
    # library itself, not an attribute on the hf module.
    def test_uses_cached_tokenizer(self) -> None:
        fake_tok = MagicMock()
        fake_tok.encode.return_value = [1, 2, 3, 4, 5]

        with patch(
            "transformers.AutoTokenizer"
        ) as mock_auto:
            mock_auto.from_pretrained.return_value = fake_tok
            msgs = [Message(role="user", parts=[TextPart(text="hello")])]
            n1 = count_tokens_hf(model="llama3.2", messages=msgs, tools=None)
            n2 = count_tokens_hf(model="llama3.2", messages=msgs, tools=None)
            assert n1 == n2 == 5
            assert mock_auto.from_pretrained.call_count == 1

    def test_falls_back_on_load_failure(self) -> None:
        with patch(
            "transformers.AutoTokenizer"
        ) as mock_auto:
            mock_auto.from_pretrained.side_effect = OSError("not on hub")
            msgs = [Message(role="user", parts=[TextPart(text="hello")])]
            n = count_tokens_hf(model="unknown-model", messages=msgs, tools=None)
            # Char fallback: 8 + ceil(5/4)=2 = 10
            assert n == 10

    def test_different_models_use_different_tokenizers(self) -> None:
        fake_a = MagicMock(); fake_a.encode.return_value = [1] * 3
        fake_b = MagicMock(); fake_b.encode.return_value = [1] * 7
        with patch("transformers.AutoTokenizer") as mock_auto:
            mock_auto.from_pretrained.side_effect = [fake_a, fake_b]
            msgs = [Message(role="user", parts=[TextPart(text="x")])]
            na = count_tokens_hf(model="llama3.2", messages=msgs, tools=None)
            nb = count_tokens_hf(model="qwen2.5", messages=msgs, tools=None)
            assert na == 3
            assert nb == 7


def test_count_tokens_hf_falls_back_without_transformers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With transformers uninstalled, counting degrades to the char heuristic.

    transformers moved to the optional 'huggingface' extra, so a core
    install has no exact tokenizer. That must cost accuracy, not raise:
    Ollama token counting is on the hot path of every turn.

    The count alone does not prove this, because a failed Hub load falls
    back to the same number. The log discriminates: the absent-dependency
    path is a debug about the extra, not a warning about a load failure.
    """
    import logging
    import sys

    from primer.llm._tokenizer import hf as hf_mod
    from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback

    hf_mod.invalidate_hf_cache()
    # sys.modules[name] = None makes `import name` raise ImportError, which
    # is how an absent extra presents at the lazy import site.
    monkeypatch.setitem(sys.modules, "transformers", None)
    messages = [Message(role="user", parts=[TextPart(text="hello world")])]

    with caplog.at_level(logging.DEBUG, logger="primer.llm._tokenizer.hf"):
        got = hf_mod.count_tokens_hf(model="llama3", messages=messages)

    assert got == count_tokens_char_fallback(messages=messages, tools=None)
    assert any("not installed" in r.getMessage() for r in caplog.records), (
        "expected the absent-dependency path, not a Hub load failure"
    )


def test_hf_module_does_not_import_transformers_eagerly() -> None:
    """The import must be inside _get_tokenizer, not at module scope.

    A module-level import would drag transformers into every core install
    the moment anything touches the Ollama tokenizer path, which is the
    dependency this task removes.
    """
    from primer.llm._tokenizer import hf as hf_mod

    assert not hasattr(hf_mod, "AutoTokenizer")
