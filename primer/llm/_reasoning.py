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
agent node) applies the profile's config through :func:`profile_stream_kwargs`:
the adapter translates it for its own wire
(:meth:`primer.int.llm.LLM.profile_extended`, which calls the mapping for its
kind here) and the result is sent as ``stream(extended=...)``. A call with no
config, or a config that asks for nothing, is made exactly as before.
"""

from __future__ import annotations

import logging
from typing import Any

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


def profile_extended(llm: Any, config: ModelProfileConfig | None) -> dict[str, Any]:
    """The ``extended`` knobs a call under a profile's ``config`` sends through ``llm``.

    The adapter translates for its own wire
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
    return translate(config)


def profile_stream_kwargs(llm: Any, config: ModelProfileConfig | None) -> dict[str, Any]:
    """The keyword arguments a call under ``config`` adds to ``llm.stream(...)``.

    ``{"extended": {...}}`` with the profile's settings translated for
    ``llm``'s wire, or ``{}`` when there is nothing to send, so a call with no
    config (or an empty one) is made exactly as before.
    """
    extended = profile_extended(llm, config)
    return {"extended": extended} if extended else {}


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
    "profile_stream_kwargs",
]
