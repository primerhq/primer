"""Conservative character-heuristic token counter.

Last-resort floor used when a provider-native counter is unavailable
or fails. Roughly ``sum(len(text_serialisation)) / 4`` plus per-part
overhead constants borrowed from
:class:`primer.agent.compaction.CompactionStrategy._estimate_tokens`
so the two estimators agree on order of magnitude.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from primer.model.chat import (
    Message,
    Part,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)
from primer.common.identity_memo import IdentityMemo
from primer.model.media_tokens import media_tokens


# Serialising a call's arguments or a tool's schema is the only costly step of an estimate, and a turn estimates the same objects every round.
_CALL_MEMO = IdentityMemo()
_TOOL_MEMO = IdentityMemo()


# Ideographs, kana and hangul cost about one token per character in a real vocabulary (the chars/4 rule undercounts them about 1.8x, GI-19); everything else
# keeps a quarter token per character, so a Latin or Cyrillic estimate is unchanged.
_CJK = re.compile("[\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U0002fa1f]")


def script_chars(text: str) -> tuple[int, int]:
    """``(cjk, other)``: the characters of text that cost a token each, and the rest."""
    if text.isascii():
        return 0, len(text)
    cjk = sum(1 for _ in _CJK.finditer(text))
    return cjk, len(text) - cjk


def _tokens(cjk: int, other: int) -> int:
    return cjk + -(-other // 4)


def text_tokens(text: str) -> int:
    """The estimate for one piece of text (the compaction estimator and the fallback counter share it)."""
    return _tokens(*script_chars(text))


def _estimate_part(part: Part) -> int:
    if isinstance(part, TextPart):
        return text_tokens(part.text)
    if isinstance(part, ToolCallPart):
        cjk, other = _CALL_MEMO.get(
            part, (id(part.arguments), len(part.arguments)),
            lambda: script_chars(json.dumps(part.arguments, ensure_ascii=False)),
        )
        name_cjk, name_other = script_chars(part.name)
        return 50 + name_cjk + name_other + _tokens(cjk, other)
    if isinstance(part, ToolResultPart):
        return 20 + text_tokens(part.output)
    media = media_tokens(part)
    if media is not None:
        return media
    return 200


def _estimate_tool(tool: Tool) -> int:
    schema_cjk, schema_other = _TOOL_MEMO.get(
        tool, (id(tool.args_schema), len(tool.args_schema)),
        lambda: script_chars(json.dumps(tool.args_schema, ensure_ascii=False)),
    )
    id_cjk, id_other = script_chars(tool.id)
    desc_cjk, desc_other = script_chars(tool.description or "")
    return 50 + _tokens(schema_cjk + id_cjk + desc_cjk, schema_other + id_other + desc_other)


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
