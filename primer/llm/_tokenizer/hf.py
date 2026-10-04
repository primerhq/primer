"""HuggingFace ``transformers.AutoTokenizer`` counter for Ollama models.

An exact-ish prompt count, locally, with no network on the turn path.

This module RAISES :class:`~primer.model.except_.TokenCounterUnavailable`
whenever it cannot count; ``primer.llm.counting.count_prompt_tokens`` is the one
place that turns that into a labelled estimate. It used to return the character
heuristic as a successful count (labelling an estimate native), and it called
``AutoTokenizer.from_pretrained(<ollama model name>)`` on EVERY count: an Ollama
name such as ``llama3.1:8b`` is not a Hub repo id, so that was a hub lookup per
turn that failed, logged a WARNING and was never cached.

Now:

* only a name that looks like a Hub repo id (``org/name``) is tried at all; every
  other name is unavailable, silently, and remembered;
* the tokenizer is loaded with ``local_files_only=True``: an already-cached
  tokenizer is used, the Hub is never contacted from a turn;
* a failure (``transformers`` not installed, name not cached, load error) is
  remembered for the life of the process, so it is neither retried nor logged per
  turn.

The result is ``exact=False``: the tokenizer is a stand-in for whatever checkpoint
the Ollama model really is. ``transformers`` ships in the optional ``huggingface``
extra and is imported lazily; the default slim image does not have it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from primer.model.chat import (
    Message,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)
from primer.model.except_ import TokenCounterUnavailable
from primer.model.media_tokens import media_tokens
from primer.model.token_count import TokenCount


logger = logging.getLogger(__name__)

# ``org/name`` as the Hub spells it. Ollama tags (``llama3.1:8b``) never match.
_REPO_ID = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")

_TOKENIZER_CACHE: dict[str, Any] = {}
# Process-lifetime negative cache: model name -> why it cannot be counted.
_UNAVAILABLE: dict[str, str] = {}


def invalidate_hf_cache() -> None:
    """Forget every loaded tokenizer and remembered failure (tests, a re-pull)."""
    _TOKENIZER_CACHE.clear()
    _UNAVAILABLE.clear()


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


def _get_tokenizer(model: str) -> Any:
    cached = _TOKENIZER_CACHE.get(model)
    if cached is not None:
        return cached
    if model in _UNAVAILABLE:
        raise TokenCounterUnavailable(_UNAVAILABLE[model])
    if not _REPO_ID.match(model):
        reason = f"hf tokenizer: {model!r} is not a Hub repo id (org/name)"
        _UNAVAILABLE[model] = reason
        raise TokenCounterUnavailable(reason)
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        # ImportError, not ModuleNotFoundError: the sys.modules[name]=None
        # idiom used to simulate absence raises the plain base class.
        reason = (
            "hf tokenizer: transformers is not installed "
            "(install 'primer-ai[huggingface]' for local Ollama token counts)"
        )
        _UNAVAILABLE[model] = reason
        raise TokenCounterUnavailable(reason, cause=exc) from exc
    try:
        tok = AutoTokenizer.from_pretrained(model, local_files_only=True)
    except Exception as exc:  # noqa: BLE001 - not cached, a bad repo id, a corrupt file
        reason = (
            f"hf tokenizer: {model!r} is not available locally "
            f"({type(exc).__name__}); not contacting the Hub from a turn"
        )
        _UNAVAILABLE[model] = reason
        logger.warning("%s; native counts disabled for it until restart", reason)
        raise TokenCounterUnavailable(reason, cause=exc) from exc
    _TOKENIZER_CACHE[model] = tok
    return tok


def count_tokens_hf_detailed(
    *,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
) -> TokenCount:
    """Token count via the model's local HF tokenizer (synchronous, CPU-bound).

    Media blocks are not in the serialised text; they are estimated with the
    shared constants and reported as an estimated component. Raises
    :class:`TokenCounterUnavailable`; never returns a heuristic number.
    """
    tok = _get_tokenizer(model)
    text = _serialise(messages, tools)
    media = sum(
        estimate
        for msg in messages
        for part in msg.parts
        if (estimate := media_tokens(part)) is not None
    )
    return TokenCount(
        total=len(tok.encode(text)) + media,
        exact=False,
        estimated_components=("media",) if media else (),
        encoding=model,
    )


__all__ = [
    "count_tokens_hf_detailed",
    "invalidate_hf_cache",
    "_TOKENIZER_CACHE",
]
