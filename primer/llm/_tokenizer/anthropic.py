"""Anthropic ``messages.count_tokens`` adapter with LRU cache.

The Anthropic SDK exposes a dedicated count-tokens endpoint that
returns an exact prompt-token count for ``(model, messages, tools)``.
Each call costs one network round-trip, so we cache aggressively
keyed on ``(model, sha256(messages), sha256(tools))``.

On API failure the counter logs a WARNING and falls back to the
char-heuristic floor — compaction must never block a chat turn on
a transient counter error.
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
    DocumentPart,
    ImagePart,
    Message,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)


logger = logging.getLogger(__name__)


_CACHE_MAX = 1024
_CACHE: "OrderedDict[str, int]" = OrderedDict()


def invalidate_anthropic_cache() -> None:
    """Drop every cached entry. Test seam; production code never calls this."""
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


def _to_anthropic_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Down-translate to the API's ``{role, content}`` wire shape."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            continue
        content: list[dict[str, Any]] = []
        for part in msg.parts:
            if isinstance(part, TextPart):
                content.append({"type": "text", "text": part.text})
            elif isinstance(part, ToolCallPart):
                content.append({
                    "type": "tool_use",
                    "id": part.id,
                    "name": part.name,
                    "input": part.arguments,
                })
            elif isinstance(part, ToolResultPart):
                content.append({
                    "type": "tool_result",
                    "tool_use_id": part.id,
                    "content": part.output,
                    "is_error": getattr(part, "error", False),
                })
            elif isinstance(part, ImagePart):
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.mime_type or "image/png",
                        "data": "",
                    },
                })
            elif isinstance(part, DocumentPart):
                content.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": part.mime_type or "application/pdf",
                        "data": "",
                    },
                })
        if content:
            out.append({"role": msg.role, "content": content})
    return out


def _to_anthropic_tools(tools: Sequence[Tool] | None) -> list[dict[str, Any]]:
    if not tools:
        return []
    return [
        {
            "name": t.id,
            "description": t.description or "",
            "input_schema": t.args_schema,
        }
        for t in tools
    ]


async def count_tokens_anthropic(
    *,
    client: Any,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
) -> int:
    """Exact prompt-token count via Anthropic's count-tokens endpoint.

    Cached on ``(model, hash(messages), hash(tools))``. On any
    exception the counter logs WARNING and falls back to the char
    heuristic — never raises.
    """
    cache_key = f"{model}|{_hash_messages(messages)}|{_hash_tools(tools)}"
    if cache_key in _CACHE:
        _CACHE.move_to_end(cache_key)
        return _CACHE[cache_key]

    try:
        result = await client.messages.count_tokens(
            model=model,
            messages=_to_anthropic_messages(messages),
            tools=_to_anthropic_tools(tools),
        )
        count = int(result.input_tokens)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "anthropic count_tokens failed; using char fallback: %s", exc,
        )
        return count_tokens_char_fallback(messages=messages, tools=tools)

    _CACHE[cache_key] = count
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return count


__all__ = [
    "count_tokens_anthropic",
    "invalidate_anthropic_cache",
    "_CACHE_MAX",
]
