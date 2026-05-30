"""Tiktoken-backed token counter for OpenAI-Responses + OpenChat adapters.

Maps model names to the right ``tiktoken`` encoding. ``o200k_base``
covers the gpt-4o / o1 / o3 / o4 families; ``cl100k_base`` covers
legacy gpt-3.5-turbo / gpt-4 / gpt-4-turbo. Unknown models default
to ``o200k_base`` (the current ChatGPT default).

Serialises messages and tools to a canonical text form, then encodes
once and returns the token length. Per-message envelope overhead
(``4``) mirrors OpenAI's published cookbook recipe for chat models.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import lru_cache

import tiktoken

from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ExtendedPart,
    ImagePart,
    Message,
    Part,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
    VideoPart,
)


# o200k_base: gpt-4o, gpt-4o-mini, o1, o1-mini, o3, o3-mini, o4-mini
# cl100k_base: gpt-4, gpt-4-turbo, gpt-3.5-turbo (and legacy chat models)
_MODEL_TO_ENCODING: dict[str, str] = {
    "gpt-4o": "o200k_base",
    "gpt-4o-mini": "o200k_base",
    "gpt-4o-2024-05-13": "o200k_base",
    "gpt-4o-2024-08-06": "o200k_base",
    "o1": "o200k_base",
    "o1-mini": "o200k_base",
    "o1-preview": "o200k_base",
    "o3": "o200k_base",
    "o3-mini": "o200k_base",
    "o4-mini": "o200k_base",
    "gpt-4": "cl100k_base",
    "gpt-4-turbo": "cl100k_base",
    "gpt-4-32k": "cl100k_base",
    "gpt-3.5-turbo": "cl100k_base",
}


def resolve_encoding_name(model: str) -> str:
    """Map a model name (possibly prefixed by provider) to a tiktoken encoding."""
    key = model.lower().split("/")[-1]
    return _MODEL_TO_ENCODING.get(key, "o200k_base")


@lru_cache(maxsize=8)
def _get_encoding(name: str) -> tiktoken.Encoding:
    return tiktoken.get_encoding(name)


def _part_text(part: Part) -> str:
    if isinstance(part, TextPart):
        return part.text
    if isinstance(part, ToolCallPart):
        return f"{part.name}({json.dumps(part.arguments, ensure_ascii=False)})"
    if isinstance(part, ToolResultPart):
        return f"[result:{part.id}] {part.output}"
    if isinstance(part, ImagePart):
        return "[image] " + ("x" * 4000)
    if isinstance(part, DocumentPart):
        return "[document] " + ("x" * 8000)
    if isinstance(part, ExtendedPart):
        inner = part.extended
        if isinstance(inner, (AudioPart, VideoPart)):
            return "[av] " + ("x" * 6000)
        return "[extended] " + ("x" * 2000)
    return "[unknown]"


def _serialise_messages(messages: Sequence[Message]) -> str:
    chunks: list[str] = []
    for msg in messages:
        chunks.append(f"<|role:{msg.role}|>")
        for part in msg.parts:
            chunks.append(_part_text(part))
    return "\n".join(chunks)


def _serialise_tools(tools: Sequence[Tool] | None) -> str:
    if not tools:
        return ""
    out: list[str] = []
    for tool in tools:
        out.append(
            json.dumps(
                {
                    "name": tool.id,
                    "description": tool.description or "",
                    "input_schema": tool.args_schema,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return "\n".join(out)


def count_tokens_openai(
    *,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
) -> int:
    """Token count via the model's tiktoken encoding.

    Per-message overhead of 4 tokens covers role + envelope markers,
    matching OpenAI's published cookbook recipe.
    """
    encoding = _get_encoding(resolve_encoding_name(model))
    payload = _serialise_messages(messages)
    tools_payload = _serialise_tools(tools)
    tokens = len(encoding.encode(payload))
    if tools_payload:
        tokens += len(encoding.encode(tools_payload))
    tokens += 4 * len(messages)
    return tokens


__all__ = [
    "count_tokens_openai",
    "resolve_encoding_name",
    "_MODEL_TO_ENCODING",
]
