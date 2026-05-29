"""Shared helpers for the OpenAI-backed LLM adapters.

The ``openresponses`` and ``openchat`` adapters both wrap the
``openai.AsyncOpenAI`` SDK but target different wire formats. Anything
that is identical between them lives here so we don't grow two copies
that drift over time.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)


SamplingTarget = Literal["responses", "chat_completions"]


def build_sampling_params(
    *,
    temperature: float | None,
    top_p: float | None,
    max_output_tokens: int | None,
    stop: list[str] | None,
    target: SamplingTarget,
) -> dict[str, Any]:
    """Forward the universal sampling knobs to a target wire format.

    The two OpenAI wire formats differ in two ways:

    * Token cap key - Responses uses ``max_output_tokens``; Chat
      Completions uses ``max_tokens``.
    * ``stop`` - Chat Completions accepts it natively; Responses does
      not and we warn once when callers pass it.

    Pass ``target="responses"`` from the openresponses adapter, or
    ``target="chat_completions"`` from the openchat adapter.
    """
    params: dict[str, Any] = {}
    if temperature is not None:
        params["temperature"] = temperature
    if top_p is not None:
        params["top_p"] = top_p
    if max_output_tokens is not None:
        if target == "chat_completions":
            params["max_tokens"] = max_output_tokens
        else:
            params["max_output_tokens"] = max_output_tokens
    if stop is not None:
        if target == "chat_completions":
            params["stop"] = stop
        else:
            logger.warning(
                "OpenAI Responses API does not support 'stop' parameter; "
                "ignoring stop=%r",
                stop,
            )
    return params
