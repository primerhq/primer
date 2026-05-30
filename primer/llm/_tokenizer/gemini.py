"""Gemini ``models.count_tokens`` adapter with LRU cache.

google-genai exposes ``client.aio.models.count_tokens(model=..., contents=...)``
which returns ``CountTokensResponse(total_tokens=int)``. The call is
a network round-trip; we cache on ``(model, hash(messages), hash(tools))``
to keep the auto-trigger hot path fast.

Failure falls back to the char heuristic — never blocks a turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import (
    Message,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)


logger = logging.getLogger(__name__)


_CACHE_MAX = 1024
_CACHE: "OrderedDict[str, int]" = OrderedDict()


def invalidate_gemini_cache() -> None:
    _CACHE.clear()


def _hash_messages(messages: Sequence[Message]) -> str:
    payload = [m.model_dump(mode="json") for m in messages]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _hash_tools(tools: Sequence[Tool] | None) -> str:
    if not tools:
        return "none"
    payload = [t.model_dump(mode="json") for t in tools]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _to_gemini_contents(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Translate to the google-genai ``contents`` shape."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            continue
        role = "model" if msg.role == "assistant" else msg.role
        parts: list[dict[str, Any]] = []
        for part in msg.parts:
            if isinstance(part, TextPart):
                parts.append({"text": part.text})
            elif isinstance(part, ToolCallPart):
                parts.append({
                    "function_call": {
                        "name": part.name,
                        "args": part.arguments,
                    }
                })
            elif isinstance(part, ToolResultPart):
                parts.append({
                    "function_response": {
                        "name": part.id,
                        "response": {"result": part.output},
                    }
                })
        if parts:
            out.append({"role": role, "parts": parts})
    return out


async def count_tokens_gemini(
    *,
    client: Any,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
) -> int:
    """Exact prompt-token count via Gemini's count-tokens endpoint."""
    cache_key = f"{model}|{_hash_messages(messages)}|{_hash_tools(tools)}"
    if cache_key in _CACHE:
        _CACHE.move_to_end(cache_key)
        return _CACHE[cache_key]

    try:
        result = await client.aio.models.count_tokens(
            model=model,
            contents=_to_gemini_contents(messages),
        )
        count = int(result.total_tokens)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "gemini count_tokens failed; using char fallback: %s", exc,
        )
        return count_tokens_char_fallback(messages=messages, tools=tools)

    _CACHE[cache_key] = count
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return count


__all__ = [
    "count_tokens_gemini",
    "invalidate_gemini_cache",
    "_CACHE_MAX",
]
