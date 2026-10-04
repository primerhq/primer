"""Conservative character-heuristic token counter.

Last-resort floor used when a provider-native counter is unavailable
or fails. Roughly ``sum(len(text_serialisation)) / 4`` plus per-part
overhead constants borrowed from
:class:`primer.agent.compaction.CompactionStrategy._estimate_tokens`
so the two estimators agree on order of magnitude.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from primer.model.chat import (
    Message,
    Part,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)
from primer.model.media_tokens import media_tokens


def _estimate_part(part: Part) -> int:
    if isinstance(part, TextPart):
        return -(-len(part.text) // 4)
    if isinstance(part, ToolCallPart):
        args_len = len(json.dumps(part.arguments, ensure_ascii=False))
        return 50 + len(part.name) + -(-args_len // 4)
    if isinstance(part, ToolResultPart):
        return 20 + -(-len(part.output) // 4)
    media = media_tokens(part)
    if media is not None:
        return media
    return 200


def _estimate_tool(tool: Tool) -> int:
    schema_chars = len(json.dumps(tool.args_schema, ensure_ascii=False))
    name_chars = len(tool.id)
    desc_chars = len(tool.description or "")
    return 50 + -(-(name_chars + desc_chars + schema_chars) // 4)


def count_tokens_char_fallback(
    *,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
) -> int:
    """Last-resort floor: per-part char heuristic.

    See :class:`primer.agent.compaction.CompactionStrategy._estimate_tokens`
    for the per-part overhead constants this mirrors.
    """
    total = 0
    for msg in messages:
        total += 8
        for part in msg.parts:
            total += _estimate_part(part)
    if tools:
        for tool in tools:
            total += _estimate_tool(tool)
    return total


__all__ = ["count_tokens_char_fallback"]
