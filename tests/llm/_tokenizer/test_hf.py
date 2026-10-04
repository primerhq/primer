"""HF-tokenizer counter (Ollama): local files only, remembers failures, raises.

It used to call ``AutoTokenizer.from_pretrained(<ollama model name>)`` on every
count (a Hub lookup per turn, for a name that is not a repo id), return the
character heuristic as a successful count on failure, and log a WARNING each time.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

from primer.llm._tokenizer import hf as hf_mod
from primer.llm._tokenizer.hf import count_tokens_hf_detailed, invalidate_hf_cache
from primer.model.chat import ImagePart, Message, TextPart
from primer.model.except_ import TokenCounterUnavailable
from primer.model.media_tokens import IMAGE_TOKENS

REPO = "Qwen/Qwen2.5-7B-Instruct"
MSGS = [Message(role="user", parts=[TextPart(text="hello")])]


@pytest.fixture(autouse=True)
def _clear() -> None:
    invalidate_hf_cache()
    yield
    invalidate_hf_cache()


@pytest.fixture
def fake_transformers(monkeypatch):
    """A stand-in ``transformers`` so these run with or without the extra."""
    auto = MagicMock()
    module = types.ModuleType("transformers")
    module.AutoTokenizer = auto
    monkeypatch.setitem(sys.modules, "transformers", module)
    return auto


def _tok(n: int) -> MagicMock:
    tok = MagicMock()
    tok.encode.return_value = [1] * n
    return tok


def test_a_cached_repo_tokenizer_counts_and_is_never_exact(fake_transformers) -> None:
    fake_transformers.from_pretrained.return_value = _tok(5)
    got = count_tokens_hf_detailed(model=REPO, messages=MSGS)
    assert (got.total, got.exact, got.estimated_components) == (5, False, ())
    assert got.encoding == REPO


def test_the_hub_is_never_contacted_from_a_turn(fake_transformers) -> None:
    fake_transformers.from_pretrained.return_value = _tok(1)
    count_tokens_hf_detailed(model=REPO, messages=MSGS)
    assert fake_transformers.from_pretrained.call_args.kwargs == {"local_files_only": True}


def test_the_tokenizer_is_loaded_once_per_model(fake_transformers) -> None:
    fake_transformers.from_pretrained.return_value = _tok(5)
    for _ in range(3):
        count_tokens_hf_detailed(model=REPO, messages=MSGS)
    assert fake_transformers.from_pretrained.call_count == 1


def test_different_models_use_different_tokenizers(fake_transformers) -> None:
    fake_transformers.from_pretrained.side_effect = [_tok(3), _tok(7)]
    assert count_tokens_hf_detailed(model="a/one", messages=MSGS).total == 3
    assert count_tokens_hf_detailed(model="b/two", messages=MSGS).total == 7


@pytest.mark.parametrize("name", ["llama3.1:8b", "llama3.2", "mistral:latest"])
def test_an_ollama_tag_is_not_a_hub_repo_id_and_never_reaches_transformers(
    fake_transformers, name,
) -> None:
    with pytest.raises(TokenCounterUnavailable, match="not a Hub repo id"):
        count_tokens_hf_detailed(model=name, messages=MSGS)
    fake_transformers.from_pretrained.assert_not_called()


def test_an_unavailable_model_is_remembered_not_retried(fake_transformers, caplog) -> None:
    fake_transformers.from_pretrained.side_effect = OSError("not cached")
    with caplog.at_level(logging.WARNING, logger="primer.llm._tokenizer.hf"):
        for _ in range(3):
            with pytest.raises(TokenCounterUnavailable, match="not available locally"):
                count_tokens_hf_detailed(model=REPO, messages=MSGS)
    assert fake_transformers.from_pretrained.call_count == 1
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_missing_transformers_is_unavailable_and_remembered(monkeypatch) -> None:
    # sys.modules[name] = None makes `import name` raise ImportError, which is
    # how an absent extra presents at the lazy import site.
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(TokenCounterUnavailable, match="not installed"):
        count_tokens_hf_detailed(model=REPO, messages=MSGS)
    with pytest.raises(TokenCounterUnavailable, match="not installed"):
        count_tokens_hf_detailed(model=REPO, messages=MSGS)


def test_media_is_estimated_not_serialised(fake_transformers) -> None:
    fake_transformers.from_pretrained.return_value = _tok(4)
    got = count_tokens_hf_detailed(model=REPO, messages=[Message(role="user", parts=[
        TextPart(text="look"), ImagePart(mime_type="image/png", data=b"\x00"),
    ])])
    assert got.total == 4 + IMAGE_TOKENS
    assert got.estimated_components == ("media",)


def test_it_never_returns_a_heuristic_for_a_failure(fake_transformers) -> None:
    fake_transformers.from_pretrained.side_effect = OSError("x")
    with pytest.raises(TokenCounterUnavailable):
        count_tokens_hf_detailed(model=REPO, messages=MSGS)


def test_hf_module_does_not_import_transformers_eagerly() -> None:
    """The import must be inside _get_tokenizer, not at module scope: a
    module-level import would drag transformers into every core install."""
    assert not hasattr(hf_mod, "AutoTokenizer")
