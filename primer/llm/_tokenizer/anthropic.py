"""Anthropic ``messages.count_tokens`` counter.

The Anthropic SDK exposes a count-tokens endpoint that returns the exact
prompt-token count for ``(model, system, messages, tools)``. One network round
trip per call, so it is meant to run rarely (the turn path asks the provider's
own usage first and only counts when that is unavailable).

This module RAISES. A mapped provider error (``classify_anthropic_exception``)
or ``TokenCounterUnavailable`` carries every failure to
``primer.llm.counting.count_prompt_tokens``, the one place that decides what a
turn does about it. It used to swallow every exception and return the character
heuristic as a successful count, which labelled an estimate as a native count
and made the wrapper's fallback outcomes unreachable.

What is sent, and what is not:

* ``system`` messages are lifted into the endpoint's ``system`` parameter
  (they used to be dropped, undercounting every prompt by its system prompt).
* Media blocks are NOT sent. The endpoint cannot count bytes primer does not
  put on the wire, and an empty base64 placeholder (what this module used to
  send) is unverified against the API and likely rejected. Each media block is
  estimated with the shared flat constants and reported as an estimated
  component.

Bounded: a per-call ``timeout`` and ``max_retries=0`` (the SDK default is a 600 s
read timeout and two retries, so a stalled count could hold a turn for minutes).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from primer.common.anthropic_errors import classify_anthropic_exception
from primer.model.chat import (
    Message,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)
from primer.model.except_ import PrimerError, TokenCounterUnavailable
from primer.model.media_tokens import media_tokens
from primer.model.token_count import TokenCount

COUNT_TIMEOUT_S = 3.0


def _to_anthropic_request(
    messages: Sequence[Message],
) -> tuple[str | None, list[dict[str, Any]], int]:
    """``(system, wire messages, media estimate)`` for the count endpoint."""
    system_chunks: list[str] = []
    out: list[dict[str, Any]] = []
    media = 0
    for msg in messages:
        if msg.role == "system":
            system_chunks.extend(p.text for p in msg.parts if isinstance(p, TextPart))
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
            else:
                estimate = media_tokens(part)
                if estimate is not None:
                    media += estimate
        if content:
            out.append({"role": msg.role, "content": content})
    system = "\n\n".join(system_chunks) if system_chunks else None
    return system, out, media


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


async def count_tokens_anthropic_detailed(
    *,
    client: Any,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
    timeout_s: float = COUNT_TIMEOUT_S,
) -> TokenCount:
    """Prompt-token count via Anthropic's count-tokens endpoint.

    ``exact`` (it is the vendor's own count) with ``media`` listed as an
    estimated component when media blocks were estimated rather than counted.
    Raises a mapped provider error, or :class:`TokenCounterUnavailable` when
    nothing natively countable remains (an all-media or empty prompt).
    """
    system, wire_messages, media = _to_anthropic_request(messages)
    if not wire_messages:
        raise TokenCounterUnavailable(
            "anthropic count_tokens: nothing natively countable in the prompt "
            "(only system text or media), which the endpoint rejects"
        )
    request: dict[str, Any] = {
        "model": model,
        "messages": wire_messages,
        "tools": _to_anthropic_tools(tools),
        "timeout": timeout_s,
    }
    if system is not None:
        request["system"] = system
    scoped = client.with_options(max_retries=0) if hasattr(client, "with_options") else client
    try:
        result = await scoped.messages.count_tokens(**request)
        counted = int(result.input_tokens)
    except PrimerError:
        raise
    except Exception as exc:
        raise classify_anthropic_exception(exc) from exc
    return TokenCount(
        total=counted + media,
        exact=True,
        estimated_components=("media",) if media else (),
    )


__all__ = ["COUNT_TIMEOUT_S", "count_tokens_anthropic_detailed"]
