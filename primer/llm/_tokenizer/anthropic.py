"""Anthropic ``messages.count_tokens`` counter.

The Anthropic SDK exposes a count-tokens endpoint that returns the exact
prompt-token count for ``(model, system, messages, tools)``. One network round
trip per call, so it is meant to run rarely (the turn path asks the provider's
own usage first and only counts when that is unavailable).

This module RAISES. A mapped provider error (``classify_anthropic_exception``) or
``TokenCounterUnavailable`` carries every failure to
``primer.llm.counting.count_prompt_tokens``, the one place that decides what a
turn does about it. It used to swallow every exception and return the character
heuristic as a successful count, which labelled an estimate as a native count
and made the wrapper's fallback outcomes unreachable.

The request is built by the SAME translators the live ``stream()`` uses (passed in
by the adapter, so this module never imports it): the count API accepts only
``user`` and ``assistant`` roles, and ``stream()`` already turns a ``tool`` message
into a user-role ``tool_result`` block. A private copy of that walk would drift
from the real request, and did: it sent role ``tool`` verbatim, which the API
rejects, so every count of a tool-using history failed.

What is sent, and what is not:

* ``system`` messages go in the endpoint's ``system`` parameter (the live
  translator lifts them).
* Media is NOT sent. The endpoint cannot count bytes primer does not put on the
  wire, and an empty base64 placeholder is unverified and likely rejected. Each
  media block is removed before translation, estimated with the shared flat
  constants, and reported as an estimated component.

Bounded: a per-call ``timeout`` and ``max_retries=0`` (the SDK default is a 600 s
read timeout and two retries, so a stalled count could hold a turn for minutes).
A timeout is a ``ProviderTimeoutError`` (outcome ``fallback_timeout``); an unclassified
4xx is a ``BadRequestError`` (a rejection), never a bare ``ProviderError``, which
the wrapper would treat as a bug.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import anthropic

from primer.common.anthropic_errors import classify_anthropic_exception
from primer.llm._tokenizer._errors import promote_unclassified_4xx
from primer.model.chat import Message, Tool
from primer.model.except_ import (
    PrimerError,
    ProviderTimeoutError,
    TokenCounterUnavailable,
)
from primer.model.media_tokens import split_media
from primer.model.token_count import TokenCount

COUNT_TIMEOUT_S = 3.0

MessagesToWire = Callable[[list[Message]], "tuple[str | None, list[dict[str, Any]]]"]
ToolsToWire = Callable[[list[Tool] | None], "list[dict[str, Any]] | None"]


def _map_error(exc: Exception) -> PrimerError:
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeoutError(
            f"anthropic count_tokens timed out ({type(exc).__name__})", cause=exc,
        )
    return promote_unclassified_4xx(classify_anthropic_exception(exc))


async def count_tokens_anthropic_detailed(
    *,
    client: Any,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
    messages_to_wire: MessagesToWire,
    tools_to_wire: ToolsToWire,
    timeout_s: float = COUNT_TIMEOUT_S,
) -> TokenCount:
    """Prompt-token count via Anthropic's count-tokens endpoint.

    ``exact`` (it is the vendor's own count) with ``media`` listed as an
    estimated component when media blocks were estimated rather than counted.
    Raises a mapped provider error, or :class:`TokenCounterUnavailable` when
    nothing natively countable remains (an all-media or empty prompt).
    """
    stripped, media = split_media(list(messages))
    system, wire_messages = messages_to_wire(stripped)
    if not wire_messages:
        raise TokenCounterUnavailable(
            "anthropic count_tokens: nothing natively countable in the prompt "
            "(only system text or media), which the endpoint rejects"
        )
    request: dict[str, Any] = {
        "model": model,
        "messages": wire_messages,
        "timeout": timeout_s,
    }
    wire_tools = tools_to_wire(list(tools) if tools else None)
    if wire_tools:
        request["tools"] = wire_tools
    if system is not None:
        request["system"] = system
    scoped = client.with_options(max_retries=0) if hasattr(client, "with_options") else client
    try:
        result = await scoped.messages.count_tokens(**request)
        counted = int(result.input_tokens)
    except PrimerError:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc
    return TokenCount(
        total=counted + media,
        exact=True,
        estimated_components=("media",) if media else (),
    )


__all__ = ["COUNT_TIMEOUT_S", "count_tokens_anthropic_detailed"]
