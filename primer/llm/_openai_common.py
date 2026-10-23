"""Shared helpers for the OpenAI-backed LLM adapters.

The ``openresponses`` and ``openchat`` adapters both wrap the
``openai.AsyncOpenAI`` SDK but target different wire formats. Anything
that is identical between them lives here so we don't grow two copies
that drift over time.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from primer.llm._reasoning import debug_once, openai_family

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


# What OpenAI's reasoning models refuse beside a reasoning effort that is not ``none`` ("When reasoning effort is not none, remove
# temperature, top_p, and top_logprobs", OpenAI's latest-model guide). Only the call's own sampling knobs are fitted here: a
# refused ``extended`` key (``top_logprobs``, ``logprobs``) is the profile's, and the stream-open guard retries without the profile's
# settings when OpenAI refuses one, but it cannot take away a knob the call itself passed.
_REASONING_REFUSED_SAMPLING: tuple[str, ...] = ("temperature", "top_p")


def fit_openai_reasoning_request(
    request: dict[str, Any],
    *,
    flavor: str,
    target: SamplingTarget,
    adapter: str,
    provider_id: str,
    model: str,
    log: logging.Logger | None = None,
) -> None:
    """Fit, in place, a request for an OpenAI reasoning model to what OpenAI accepts (audit G2-02, G2-03, G2-04).

    Only for the ``openai`` flavor, and decided from the model's family (:func:`primer.llm._reasoning.openai_family`), not from
    whether an effort was sent: o-series, codex, gpt-5 and the later models reason by default, so a call that sends no effort
    is refused for the same settings. For such a model:

    * Chat Completions: ``max_tokens`` is sent as ``max_completion_tokens`` (the reasoning models refuse ``max_tokens``).
    * Both wires: ``temperature`` and ``top_p`` are dropped, unless the request sets the effort to ``none`` (OpenAI accepts
      them only then).

    A model that does not reason (gpt-4o) is sent as shaped. A model the table does not know (a deployment name, a model on a
    compatible server registered with the ``openai`` flavor) keeps the shape it had before the family rule: on Chat Completions
    its ``temperature`` and ``top_p`` are dropped only when an effort other than ``none`` is sent, and ``max_tokens`` stays.
    Each change is logged once at DEBUG per adapter, provider, model and change.
    """
    if flavor != "openai":
        return
    family = openai_family(model)
    if family == "no-reasoning":
        return
    if target == "chat_completions":
        effort = request.get("reasoning_effort")
    else:
        reasoning = request.get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    changed: list[str] = []
    if family is None:
        if target != "chat_completions" or effort in (None, "none"):
            return
    elif target == "chat_completions" and "max_tokens" in request:
        request["max_completion_tokens"] = request.pop("max_tokens")
        changed.append("max_tokens sent as max_completion_tokens")
    if effort != "none":
        dropped = [key for key in _REASONING_REFUSED_SAMPLING if key in request]
        for key in dropped:
            del request[key]
        if dropped:
            changed.append(", ".join(dropped) + " dropped")
    if changed:
        debug_once(
            ("openai-reasoning-fit", adapter, provider_id, model, "; ".join(changed)),
            "%s provider %s: OpenAI reasoning model %s; %s",
            adapter,
            provider_id,
            model,
            "; ".join(changed),
            log=log or logger,
        )
