"""Shared helpers for the OpenAI-backed LLM adapters.

The ``openresponses`` and ``openchat`` adapters both wrap the
``openai.AsyncOpenAI`` SDK but target different wire formats. Anything
that is identical between them lives here so we don't grow two copies
that drift over time.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from primer.llm._reasoning import debug_once, openai_has_none, openai_reasons

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


# The call's own sampling knobs an OpenAI reasoning model refuses. The rule is quoted from OpenAI's latest-model guide, the one
# written for GPT-6: "When reasoning effort is not none, remove temperature, top_p, and top_logprobs". A true ``none`` exists only on
# gpt-5.1 to 5.x (the GPT-5.1 guide makes it gpt-5.1's default effort), so the knobs are kept at ``none`` on those models alone
# (:func:`primer.llm._reasoning.openai_has_none`, ticket 01a16b64 R3): every other reasoning family starts at ``minimal`` or ``low``
# (pro takes only ``high``) and refuses them at every effort, a ``none`` from a profile's ``extended`` included. Only the call's own
# sampling knobs are fitted here: a refused ``extended`` key (``top_logprobs``, ``logprobs``) is the profile's, and the stream-open
# guard retries without the profile's settings when OpenAI refuses one, but it cannot take away a knob the call itself passed.
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

    Only for the ``openai`` flavor, and decided from whether the model reasons (:func:`primer.llm._reasoning.openai_reasons`),
    not from whether an effort was sent, nor from whether the model takes one: o-series, codex, gpt-5 and the later models
    reason by default, so a call that sends no effort is refused for the same settings, and o1-mini and o1-preview, which take
    no ``reasoning_effort`` at all, refuse them too (ticket 01a16b64 R1). For such a model:

    * Chat Completions: ``max_tokens`` is sent as ``max_completion_tokens`` (the reasoning models refuse ``max_tokens``).
    * Both wires: ``temperature`` and ``top_p`` are dropped, unless the request sets the effort to ``none`` on a model that
      has a true ``none`` (gpt-5.1 to 5.x, :func:`primer.llm._reasoning.openai_has_none`; OpenAI accepts them only then, see
      :data:`_REASONING_REFUSED_SAMPLING`). A call that sends no effort to gpt-5.1 to 5.x loses them too, on purpose (ticket
      01a16b64 R2): those models default to ``none``, but a default the request did not send is the vendor's to change, and a
      dropped knob costs only determinism where a refused one fails the call. A profile set to OFF sends ``none`` and keeps
      them.

    A model that does not reason (gpt-4o) is sent as shaped. A model the table does not know (a deployment name, a model on a
    compatible server registered with the ``openai`` flavor) keeps the shape it had before the family rule: on Chat Completions
    its ``temperature`` and ``top_p`` are dropped only when an effort other than ``none`` is sent, and ``max_tokens`` stays.
    Each change is logged once at DEBUG per adapter, provider, model and change.
    """
    if flavor != "openai":
        return
    reasons = openai_reasons(model)
    if reasons is False:
        return
    if target == "chat_completions":
        effort = request.get("reasoning_effort")
    else:
        reasoning = request.get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    changed: list[str] = []
    if reasons is None:
        if target != "chat_completions" or effort in (None, "none"):
            return
    elif target == "chat_completions" and "max_tokens" in request:
        request["max_completion_tokens"] = request.pop("max_tokens")
        changed.append("max_tokens sent as max_completion_tokens")
    if effort != "none" or not openai_has_none(model):
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
