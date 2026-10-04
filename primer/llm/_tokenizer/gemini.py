"""Gemini ``models.count_tokens`` counter.

google-genai exposes ``client.aio.models.count_tokens(model=..., contents=...)``
which returns ``CountTokensResponse(total_tokens=int)``: one network round trip,
meant to run rarely (the turn path asks the provider's own usage first).

This module RAISES. A mapped provider error (``classify_google_exception``) or
``TokenCounterUnavailable`` carries every failure to
``primer.llm.counting.count_prompt_tokens``, which alone decides what a turn does
about it. It used to swallow every exception and return the character heuristic
as a successful count.

What the endpoint can and cannot take (verified against google-genai 2.25.0 on
the Developer-API client primer builds, ``genai.Client(api_key=...)``):
``CountTokensConfig`` raises ``ValueError`` for ``system_instruction``, ``tools``
and ``generation_config`` ("only supported in Gemini Enterprise"). So this module
never sets them. It counts the CONTENTS natively and adds heuristic estimates for
the system prompt and the tool schemas (and for media, which is not sent), and
reports exactly those as estimated components. The count and the real request
therefore differ by the estimated parts, which the result says.

Bounded: a per-call ``http_options.timeout`` (milliseconds); the client itself is
built without one, so an unbounded call would otherwise be possible.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from primer.common.google_errors import classify_google_exception
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import (
    Message,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)
from primer.model.except_ import PrimerError, TokenCounterUnavailable
from primer.model.media_tokens import media_tokens
from primer.model.token_count import EstimatedComponent, TokenCount

COUNT_TIMEOUT_S = 3.0


def _to_gemini_contents(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Translate to the google-genai ``contents`` shape (system and media excluded)."""
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


async def count_tokens_gemini_detailed(
    *,
    client: Any,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
    timeout_s: float = COUNT_TIMEOUT_S,
) -> TokenCount:
    """Prompt-token count via Gemini's count-tokens endpoint, contents only.

    The system prompt, the tool schemas and any media are estimated, never sent,
    and named in ``estimated_components``. Raises a mapped provider error, or
    :class:`TokenCounterUnavailable` when there is no natively countable content.
    """
    contents = _to_gemini_contents(messages)
    if not contents:
        raise TokenCounterUnavailable(
            "gemini count_tokens: no natively countable content (the endpoint "
            "rejects an empty contents list)"
        )

    estimated = 0
    components: list[EstimatedComponent] = []

    system_messages = [m for m in messages if m.role == "system"]
    if system_messages:
        estimated += count_tokens_char_fallback(messages=system_messages)
        components.append("system")
    if tools:
        estimated += count_tokens_char_fallback(messages=[], tools=tools)
        components.append("tools")
    media = sum(
        estimate
        for msg in messages
        for part in msg.parts
        if (estimate := media_tokens(part)) is not None
    )
    if media:
        estimated += media
        components.append("media")

    try:
        result = await client.aio.models.count_tokens(
            model=model,
            contents=contents,
            config={"http_options": {"timeout": int(timeout_s * 1000)}},
        )
        counted = int(result.total_tokens)
    except PrimerError:
        raise
    except Exception as exc:
        raise classify_google_exception(exc) from exc
    return TokenCount(
        total=counted + estimated,
        exact=True,
        estimated_components=tuple(components),
    )


__all__ = ["COUNT_TIMEOUT_S", "count_tokens_gemini_detailed"]
