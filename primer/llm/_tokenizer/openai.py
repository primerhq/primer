"""Tiktoken-backed token counter for OpenAI-Responses + OpenChat adapters.

Maps model names to the right ``tiktoken`` encoding. ``o200k_base``
covers the gpt-4o / o1 / o3 / o4 families; ``cl100k_base`` covers
legacy gpt-3.5-turbo / gpt-4 / gpt-4-turbo. Unknown models default
to ``o200k_base`` (the current ChatGPT default).

Serialises messages and tools to a canonical text form, then encodes
once and returns the token length. Per-message envelope overhead
(``4``) mirrors OpenAI's published cookbook recipe for chat models.

The encodings come from :mod:`primer.llm._tokenizer._tiktoken_offline`, which
never fetches: a missing vocabulary raises ``TokenCounterUnavailable`` and the
single wrapper in ``primer.llm.counting`` owns the fallback. Text is encoded
with ``encode_ordinary``: ``encode`` raises ``ValueError`` on any content that
spells a special token (``<|endoftext|>`` in a web page or a file a tool read),
which would otherwise make the counter fail on arbitrary tool output.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from primer.llm._tokenizer import _tiktoken_offline
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
from primer.model.media_tokens import media_tokens
from primer.model.token_count import TokenCount


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


def _part_text(part: Part) -> str:
    if isinstance(part, TextPart):
        return part.text
    if isinstance(part, ToolCallPart):
        return f"{part.name}({json.dumps(part.arguments, ensure_ascii=False)})"
    if isinstance(part, ToolResultPart):
        return f"[result:{part.id}] {part.output}"
    if isinstance(part, ImagePart):
        return "[image]"
    if isinstance(part, DocumentPart):
        return "[document]"
    if isinstance(part, ExtendedPart):
        inner = part.extended
        return "[av]" if isinstance(inner, (AudioPart, VideoPart)) else "[extended]"
    return "[unknown]"


def _serialise_messages(messages: Sequence[Message]) -> str:
    chunks: list[str] = []
    for msg in messages:
        chunks.append(f"<|role:{msg.role}|>")
        for part in msg.parts:
            chunks.append(_part_text(part))
    return "\n".join(chunks)


def _media_estimate(messages: Sequence[Message]) -> int:
    """Flat estimate for the media blocks, which a text tokenizer cannot see."""
    return sum(
        estimate
        for msg in messages
        for part in msg.parts
        if (estimate := media_tokens(part)) is not None
    )


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


def is_exact_model(model: str) -> bool:
    """True when ``model`` maps to its own encoding (not the o200k default)."""
    return model.lower().split("/")[-1] in _MODEL_TO_ENCODING


def count_tokens_openai_detailed(
    *,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
) -> TokenCount:
    """Token count via the model's tiktoken encoding, with its provenance.

    Per-message overhead of 4 tokens covers role + envelope markers, matching
    OpenAI's published cookbook recipe. Media blocks are estimated with the
    shared flat constants (``primer.model.media_tokens``) and reported as an
    estimated component; ``exact`` is only True for a model that maps to its own
    encoding, so the ``o200k_base`` default for an unknown model (LM Studio,
    most OpenRouter models) is an approximation and says so.

    Raises :class:`~primer.model.except_.TokenCounterUnavailable` if the
    vocabulary is unavailable; never returns a heuristic number.
    """
    encoding_name = resolve_encoding_name(model)
    encoding = _tiktoken_offline.load_encoding(encoding_name)
    payload = _serialise_messages(messages)
    tools_payload = _serialise_tools(tools)
    tokens = len(encoding.encode_ordinary(payload))
    if tools_payload:
        tokens += len(encoding.encode_ordinary(tools_payload))
    tokens += 4 * len(messages)
    media = _media_estimate(messages)
    return TokenCount(
        total=tokens + media,
        exact=is_exact_model(model),
        estimated_components=("media",) if media else (),
        encoding=encoding_name,
    )


def count_tokens_openai(
    *,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
) -> int:
    """Token count via the model's tiktoken encoding (see the detailed form)."""
    return count_tokens_openai_detailed(
        model=model, messages=messages, tools=tools,
    ).total


async def count_openai_family(
    *,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
) -> TokenCount:
    """The adapters' shared entry point: count off the event loop."""
    from primer.llm._tokenizer._executor import run_counter

    return await run_counter(
        count_tokens_openai_detailed, model=model, messages=messages, tools=tools,
    )


__all__ = [
    "count_openai_family",
    "count_tokens_openai",
    "count_tokens_openai_detailed",
    "is_exact_model",
    "resolve_encoding_name",
    "_MODEL_TO_ENCODING",
]
