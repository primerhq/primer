"""Map the vendor-neutral :class:`ReasoningLevel` onto each vendor's wire.

No two vendors expose the same reasoning control, so the profile carries a
universal level and translation happens here, at the SDK boundary. This is
the same shape the codebase already uses for stop reasons.

Where a vendor has no true "off", the closest available setting is used and
the approximation is stated rather than hidden. Where a vendor has NO
working control at all, the mapping returns nothing and the caller warns:
silently accepting a knob that does nothing is how an operator ends up
believing they disabled reasoning when they did not.

Every model call made under a profile (an agent turn, a compaction, a graph
agent node) applies the profile's config in two steps. :func:`profile_extended`
asks the adapter to translate it for its own wire AND the model the call names
(:meth:`primer.int.llm.LLM.profile_extended`, which calls the mapping for its
kind here): the right shape is a property of the model, not only of the
provider. :func:`profile_stream` then sends the result as
``stream(extended=...)``, and a provider that refuses those settings at stream
open gets the call once more without them. A profile setting must never turn a
call that works into one that fails. A call with no config, or a config that
asks for nothing, is made exactly as before.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from primer.model.chat import Error as ChatError
from primer.model.except_ import BadRequestError
from primer.model.model_profile import ModelProfileConfig, ReasoningLevel

logger = logging.getLogger(__name__)


# OpenAI Responses has no true off; "minimal" is its floor.
_OPENAI_EFFORT: dict[ReasoningLevel, str] = {
    ReasoningLevel.OFF: "minimal",
    ReasoningLevel.MINIMAL: "minimal",
    ReasoningLevel.LOW: "low",
    ReasoningLevel.MEDIUM: "medium",
    ReasoningLevel.HIGH: "high",
}

# Gemini takes a token budget; 0 disables thinking outright.
_GEMINI_BUDGET: dict[ReasoningLevel, int] = {
    ReasoningLevel.OFF: 0,
    ReasoningLevel.MINIMAL: 512,
    ReasoningLevel.LOW: 2048,
    ReasoningLevel.MEDIUM: 8192,
    ReasoningLevel.HIGH: 24576,
}

# Anthropic: a thinking block with an explicit token budget, or disabled.
_ANTHROPIC_BUDGET: dict[ReasoningLevel, int | None] = {
    ReasoningLevel.OFF: None,
    ReasoningLevel.MINIMAL: 1024,
    ReasoningLevel.LOW: 4096,
    ReasoningLevel.MEDIUM: 16384,
    ReasoningLevel.HIGH: 32768,
}

# OpenRouter normalises reasoning across its upstreams with one ``reasoning``
# object; "none" is its documented off switch (a model whose reasoning is
# mandatory refuses it, so OFF stays a request there too).
_OPENROUTER_EFFORT: dict[ReasoningLevel, str] = {
    ReasoningLevel.OFF: "none",
    ReasoningLevel.MINIMAL: "minimal",
    ReasoningLevel.LOW: "low",
    ReasoningLevel.MEDIUM: "medium",
    ReasoningLevel.HIGH: "high",
}

# Keys :func:`debug_once` has logged in this process.
_LOGGED_ONCE: set[tuple[str, ...]] = set()


def debug_once(
    key: tuple[str, ...],
    message: str,
    *args: Any,
    log: logging.Logger | None = None,
) -> None:
    """Log ``message`` at DEBUG the first time ``key`` is seen in this process.

    For a setting a call cannot honour: the call still runs, and the reason is
    on record once rather than on every call of every turn.
    """
    if key in _LOGGED_ONCE:
        return
    _LOGGED_ONCE.add(key)
    (log or logger).debug(message, *args)


def _merge(config: ModelProfileConfig, mapped: dict[str, Any]) -> dict[str, Any]:
    """Overlay the mapped reasoning knobs on the profile's passthrough.

    The explicit ``reasoning`` level wins on a key collision: an operator
    who set both meant the typed field, and the passthrough is the escape
    hatch for things the typed field cannot say.
    """
    return {**(config.extended or {}), **mapped}


def openresponses_extended(
    config: ModelProfileConfig, *, flavor: str | None = None,
) -> dict[str, Any]:
    """Reasoning knobs for the OpenAI Responses wire.

    vLLM is the exception and it is a hard one: its Responses endpoint
    ACCEPTS ``reasoning.effort``, ``chat_template_kwargs``,
    ``extra_body.chat_template_kwargs`` and a top-level ``enable_thinking``,
    and honours none of them (verified against a live server). There is no
    knob to map, so this warns instead of emitting one that does nothing.
    An operator who needs reasoning control against vLLM must use the
    ``openchat`` provider type, where ``chat_template_kwargs`` does work.
    """
    level = config.reasoning
    if level is None:
        return dict(config.extended or {})
    if flavor == "vllm":
        logger.warning(
            "reasoning control is not supported on vLLM's Responses API; "
            "this profile's reasoning setting will have no effect. Use the "
            "'openchat' provider type for a vLLM server if you need it.",
            extra={"reasoning": level.value, "flavor": flavor},
        )
        return dict(config.extended or {})
    return _merge(config, {"reasoning_effort": _OPENAI_EFFORT[level]})


def openchat_extended(
    config: ModelProfileConfig, *, flavor: str | None = None,
) -> dict[str, Any]:
    """Reasoning knobs for the Chat Completions wire.

    vLLM (and the Qwen-family templates it serves) gates thinking through
    ``chat_template_kwargs.enable_thinking``. VERIFIED working: with it
    false the response carries ``reasoning: null`` and real content; without
    it, reasoning is emitted and ``content`` is null.

    Real OpenAI on this wire takes ``reasoning_effort``.
    """
    level = config.reasoning
    if level is None:
        return dict(config.extended or {})
    if flavor in ("vllm", "ollama", "lmstudio", "other"):
        return _merge(config, {
            "chat_template_kwargs": {
                "enable_thinking": level is not ReasoningLevel.OFF,
            },
        })
    return _merge(config, {"reasoning_effort": _OPENAI_EFFORT[level]})


def anthropic_extended(config: ModelProfileConfig) -> dict[str, Any]:
    """Reasoning knobs for the Anthropic Messages wire."""
    level = config.reasoning
    if level is None:
        return dict(config.extended or {})
    budget = _ANTHROPIC_BUDGET[level]
    if budget is None:
        return _merge(config, {"thinking": {"type": "disabled"}})
    return _merge(config, {
        "thinking": {"type": "enabled", "budget_tokens": budget},
    })


def gemini_extended(config: ModelProfileConfig) -> dict[str, Any]:
    """Reasoning knobs for the Gemini wire (a thinking-token budget)."""
    level = config.reasoning
    if level is None:
        return dict(config.extended or {})
    return _merge(config, {"thinking_budget": _GEMINI_BUDGET[level]})


def ollama_extended(config: ModelProfileConfig) -> dict[str, Any]:
    """Reasoning knobs for Ollama, which takes a plain ``think`` boolean."""
    level = config.reasoning
    if level is None:
        return dict(config.extended or {})
    return _merge(config, {"think": level is not ReasoningLevel.OFF})


def openrouter_extended(config: ModelProfileConfig) -> dict[str, Any]:
    """Reasoning knobs for OpenRouter: its ``reasoning`` object.

    OpenRouter routes one request to many upstreams and translates
    ``reasoning.effort`` for each; ``"none"`` turns reasoning off. The object
    is not a parameter of the openai SDK, so the adapter sends it in the
    request body.
    """
    level = config.reasoning
    if level is None:
        return dict(config.extended or {})
    return _merge(config, {"reasoning": {"effort": _OPENROUTER_EFFORT[level]}})


def passthrough_extended(config: ModelProfileConfig, *, adapter: str) -> dict[str, Any]:
    """What an adapter with no reasoning mapping takes from a profile.

    Its ``extended`` goes through as-is (the adapter's own whitelist still
    applies). A reasoning level cannot be applied, so it is logged once at
    DEBUG and the call goes ahead without it.
    """
    if config.reasoning is not None:
        debug_once(
            ("no-reasoning-mapping", adapter),
            "%s has no reasoning mapping; a profile's reasoning level (%s) "
            "is not applied to its calls",
            adapter, config.reasoning.value,
        )
    return dict(config.extended or {})


def profile_extended(
    llm: Any, config: ModelProfileConfig | None, *, model: str | None = None,
) -> dict[str, Any]:
    """The ``extended`` knobs a call to ``model`` under a profile's ``config`` sends through ``llm``.

    The adapter translates for its own wire and for ``model``
    (:meth:`primer.int.llm.LLM.profile_extended`); an object without that
    method (a duck-typed test double) gets the passthrough. ``None``, anything
    that is not a :class:`ModelProfileConfig`, or a config that asks for
    nothing gives ``{}`` without consulting the adapter.
    """
    if not isinstance(config, ModelProfileConfig):
        return {}
    if config.reasoning is None and not config.extended:
        return {}
    translate = getattr(llm, "profile_extended", None)
    if translate is None:
        return passthrough_extended(config, adapter=type(llm).__name__)
    return translate(config, model=model)


# Stems a provider's refusal of a reasoning setting carries, matched without
# case. The wire names differ (OpenAI "reasoning_effort" or "reasoning.effort",
# Anthropic "thinking" and "output_config.effort", Gemini "thinking_budget",
# "thinking_level" or "thinking mode", Ollama "does not support thinking",
# OpenRouter "reasoning"), so a refusal is recognised by these, or by the name
# of a key the profile sent.
_REFUSAL_STEMS: tuple[str, ...] = (
    "reason", "think", "effort", "budget", "chat_template_kwargs", "output_config",
)

# Codes that say a call failed for a reason other than what it asked for.
_NOT_A_REFUSAL: frozenset[str] = frozenset({
    "rate_limit", "server_error", "network_error", "auth_error",
    "stream_timeout", "generation_timeout", "connect_timeout",
})

# (adapter instance, model, settings) a provider refused at stream open, where
# the call then worked without them: later calls skip the settings, so a
# refused setting costs one failed request per process, not one per call.
_REFUSED: set[tuple[int, str, str]] = set()


def _names_a_setting(message: str | None, knobs: dict[str, Any]) -> bool:
    text = (message or "").lower()
    if any(stem in text for stem in _REFUSAL_STEMS):
        return True
    return any(
        re.search(rf"(?<![a-z0-9_]){re.escape(str(key).lower())}(?![a-z0-9_])", text)
        for key in knobs
    )


async def _aclose(agen: Any) -> None:
    aclose = getattr(agen, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # noqa: BLE001 -- best-effort cleanup of an abandoned stream
        pass


def profile_stream(llm: Any, knobs: dict[str, Any], **kwargs: Any) -> AsyncIterator[Any]:
    """``llm.stream(**kwargs)`` carrying a profile's translated ``knobs`` in ``extended``.

    A profile setting must never turn a call that works into one that fails.
    When the provider refuses the call at stream open (a ``BadRequestError``
    raised before the first event, or a fatal first event, which is how the
    lazily-opened Ollama and Gemini adapters report a 400) and its message
    names one of the settings, the call is made once more exactly as it would
    be without the profile's settings, and the refusal is logged once at DEBUG.
    When that call works, the model is called without those settings for the
    rest of the process. A failure after the first event is never retried:
    output has already reached the consumer.

    ``kwargs`` may carry the caller's own ``extended`` (an aggregated member's
    call does): it rides on top of the profile's settings and stays on the
    retry. No knobs: ``llm.stream(**kwargs)``, exactly as before.
    """
    passed = "extended" in kwargs
    caller = kwargs.pop("extended", None)
    plain = dict(kwargs)
    if passed:
        plain["extended"] = caller
    if not knobs:
        return llm.stream(**plain)
    key = (id(llm), str(kwargs.get("model")), json.dumps(knobs, sort_keys=True, default=str))
    if key in _REFUSED:
        return llm.stream(**plain)
    first = {**kwargs, "extended": {**knobs, **(caller or {})}}
    return _stream_with_fallback(llm, knobs, key, first, plain)


async def _stream_with_fallback(
    llm: Any,
    knobs: dict[str, Any],
    key: tuple[int, str, str],
    first_kwargs: dict[str, Any],
    plain_kwargs: dict[str, Any],
) -> AsyncIterator[Any]:
    agen = llm.stream(**first_kwargs)
    refusal = ""
    try:
        try:
            first = await agen.__anext__()
        except StopAsyncIteration:
            return
        except BadRequestError as exc:
            refusal = getattr(exc, "message", None) or str(exc)
            if not _names_a_setting(refusal, knobs):
                raise
        else:
            if not (
                isinstance(first, ChatError)
                and first.fatal
                and first.code not in _NOT_A_REFUSAL
                and _names_a_setting(first.message, knobs)
            ):
                yield first
                async for event in agen:
                    yield event
                return
            refusal = first.message
    finally:
        await _aclose(agen)
    debug_once(
        ("profile-settings-refused", type(llm).__name__, key[1], key[2]),
        "%s refused a profile's model settings for model %s (%s); the call was "
        "made again without them: %s",
        type(llm).__name__, key[1], ", ".join(sorted(map(str, knobs))),
        refusal[:300],
    )
    retry = llm.stream(**plain_kwargs)
    try:
        worked = False
        async for event in retry:
            if not worked:
                worked = not (isinstance(event, ChatError) and event.fatal)
                if worked:
                    _REFUSED.add(key)
            yield event
    finally:
        await _aclose(retry)


__all__ = [
    "anthropic_extended",
    "debug_once",
    "gemini_extended",
    "ollama_extended",
    "openchat_extended",
    "openresponses_extended",
    "openrouter_extended",
    "passthrough_extended",
    "profile_extended",
    "profile_stream",
]
