"""HuggingFace ``transformers.AutoTokenizer`` adapter for Ollama models.

Most Ollama models have a published HF tokenizer that gives an exact
prompt token count cheaply (no network — sentencepiece / BPE local).
We cache one tokenizer instance per model per process; cache miss
loads from the HF Hub (uses local cache once it's been pulled).

Load failure (model not on the Hub, hub unreachable) falls back to
the char heuristic. The cache key is the bare model name.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from transformers import AutoTokenizer

from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import (
    Message,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)


logger = logging.getLogger(__name__)


_TOKENIZER_CACHE: dict[str, Any] = {}


def invalidate_hf_cache() -> None:
    _TOKENIZER_CACHE.clear()


def _serialise(messages: Sequence[Message], tools: Sequence[Tool] | None) -> str:
    chunks: list[str] = []
    for msg in messages:
        chunks.append(f"<|{msg.role}|>")
        for part in msg.parts:
            if isinstance(part, TextPart):
                chunks.append(part.text)
            elif isinstance(part, ToolCallPart):
                chunks.append(
                    f"{part.name}({json.dumps(part.arguments, ensure_ascii=False)})"
                )
            elif isinstance(part, ToolResultPart):
                chunks.append(f"[result:{part.id}] {part.output}")
    if tools:
        for t in tools:
            chunks.append(
                json.dumps(
                    {
                        "name": t.id,
                        "description": t.description or "",
                        "input_schema": t.args_schema,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
            )
    return "\n".join(chunks)


def _get_tokenizer(model: str) -> Any | None:
    cached = _TOKENIZER_CACHE.get(model)
    if cached is not None:
        return cached
    try:
        tok = AutoTokenizer.from_pretrained(model)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hf AutoTokenizer load failed for %r: %s", model, exc,
        )
        return None
    _TOKENIZER_CACHE[model] = tok
    return tok


def count_tokens_hf(
    *,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
) -> int:
    """Token count via the model's HF tokenizer; char fallback on failure."""
    tok = _get_tokenizer(model)
    if tok is None:
        return count_tokens_char_fallback(messages=messages, tools=tools)
    text = _serialise(messages, tools)
    return len(tok.encode(text))


__all__ = [
    "count_tokens_hf",
    "invalidate_hf_cache",
    "_TOKENIZER_CACHE",
]
