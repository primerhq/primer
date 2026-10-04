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

Bounded twice. A per-call ``http_options.timeout`` (milliseconds) caps one HTTP
attempt; the client itself is built without one, so an unbounded call would
otherwise be possible. That alone is not the bound: google-genai's aiohttp path
sleeps ``1 + randint(0, 9)`` seconds and retries once after a connection error
(``_api_client.py``, 2.25.0), so one count could take the timeout, up to ten
seconds of sleep, and the timeout again. The whole call, retry included, is
therefore also wrapped in ``asyncio.wait_for(timeout_s)``, and either expiry is a
``ProviderTimeoutError``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import aiohttp
import httpx

from primer.common.google_errors import classify_google_exception
from primer.llm._tokenizer._errors import promote_unclassified_4xx
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import Message, Tool
from primer.model.except_ import (
    NetworkError,
    PrimerError,
    ProviderTimeoutError,
    TokenCounterUnavailable,
)
from primer.model.media_tokens import split_media
from primer.model.token_count import EstimatedComponent, TokenCount

COUNT_TIMEOUT_S = 3.0

MessagesToContents = Callable[[list[Message]], "tuple[str | None, list[Any]]"]


def _map_error(exc: Exception) -> PrimerError:
    """google-genai 2.x makes its async calls through aiohttp (a core dependency),
    so a stalled count raises the builtin ``TimeoutError`` (or an aiohttp subclass
    of it) and an unreachable host raises ``aiohttp.ClientConnectorError``, neither of
    which ``classify_google_exception`` (httpx only) knows. Left alone both became a
    bare ``ProviderError``: outcome ``fallback_bug``, an ERROR with a traceback, and
    never negative-cached, so every count re-stalled for the full timeout."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return ProviderTimeoutError(
            f"gemini count_tokens timed out ({type(exc).__name__})", cause=exc,
        )
    if isinstance(exc, aiohttp.ClientError):
        return NetworkError(
            f"gemini count_tokens network failure: {type(exc).__name__}",
            code="network_error", cause=exc,
        )
    return promote_unclassified_4xx(classify_google_exception(exc))


async def count_tokens_gemini_detailed(
    *,
    client: Any,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
    messages_to_contents: MessagesToContents,
    timeout_s: float = COUNT_TIMEOUT_S,
) -> TokenCount:
    """Prompt-token count via Gemini's count-tokens endpoint, contents only.

    The contents are built by the live ``stream()`` translator (passed in by the
    adapter): tool results become user-role ``function_response`` parts and the
    roles are the ones the API accepts. The system prompt, the tool schemas and any
    media are estimated, never sent, and named in ``estimated_components``. Raises
    a mapped provider error, or :class:`TokenCounterUnavailable` when there is no
    natively countable content.
    """
    stripped, media = split_media(list(messages))
    _system, contents = messages_to_contents(stripped)
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
    if media:
        estimated += media
        components.append("media")

    try:
        result = await asyncio.wait_for(
            client.aio.models.count_tokens(
                model=model,
                contents=contents,
                config={"http_options": {"timeout": int(timeout_s * 1000)}},
            ),
            timeout_s,
        )
        counted = int(result.total_tokens)
    except PrimerError:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc
    return TokenCount(
        total=counted + estimated,
        exact=True,
        estimated_components=tuple(components),
    )


__all__ = ["COUNT_TIMEOUT_S", "count_tokens_gemini_detailed"]
