"""The per-wire model settings catalogue and its validator.

Holds every wire's catalogue as data (spec section 5 of
docs/superpowers/specs/2026-11-26-profile-model-settings-design.md): the
settings a model profile may put in ``extended``, where each lands on the
wire, and the path a custom reasoning value is written to. The module
imports no vendor SDK, so the write doors (the REST router and the system
tools) can use it without pulling in adapter code.

A wire is ``"<provider_type>/<flavor>"`` for ``openchat`` and
``openresponses`` and ``"<provider_type>"`` for every other type.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

SettingType = Literal["enum", "integer", "number", "boolean", "string", "object"]

# Body fields primer sets itself; a ``body`` passthrough that names one of
# these would override the request primer builds (plan Global Constraints).
RESERVED_BODY_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "messages",
        "input",
        "tools",
        "tool_choice",
        "stream",
        "stream_options",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "temperature",
        "top_p",
        "stop",
        "instructions",
        "store",
        "previous_response_id",
    }
)


@dataclass(frozen=True)
class Setting:
    """One setting a profile may put in ``extended`` for one wire.

    ``type`` is the JSON shape the validator enforces; ``values`` (for an
    ``enum``) may mix booleans and strings; ``min``/``max`` bound numbers;
    ``fields`` are the known inner keys of an ``object``; ``open`` marks an
    object that also takes unknown keys, or an ``enum`` whose values are
    suggestions and that takes any non-empty string; ``wire_path`` is where
    the value lands on the wire (a dotted path for nesting, for example
    ``reasoning.effort``, ``body.chat_template_kwargs``, ``options.top_k``).
    """

    key: str
    type: SettingType
    description: str
    wire_path: str
    values: tuple[Any, ...] = ()
    min: float | None = None
    max: float | None = None
    fields: tuple[Setting, ...] = ()
    open: bool = False
    set_by_reasoning: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form with ``fields`` nested (for the REST surface)."""
        result: dict[str, Any] = {
            "key": self.key,
            "type": self.type,
            "description": self.description,
            "wire_path": self.wire_path,
            "values": list(self.values),
            "open": self.open,
            "set_by_reasoning": self.set_by_reasoning,
        }
        if self.min is not None:
            result["min"] = self.min
        if self.max is not None:
            result["max"] = self.max
        if self.fields:
            result["fields"] = [field.to_dict() for field in self.fields]
        return result


@dataclass(frozen=True)
class Catalogue:
    """One wire's settings and the custom reasoning target.

    ``custom_reasoning_key`` is a dotted ``extended`` path (for example
    ``output_config.effort``); the setting at that path is an open ``enum``
    (spec section 3).
    """

    wire: str
    settings: tuple[Setting, ...]
    body_passthrough: bool
    custom_reasoning_key: str

    def keys(self) -> frozenset[str]:
        """The top-level setting keys (``body`` is not a setting)."""
        return frozenset(setting.key for setting in self.settings)

    def custom_reasoning_setting(self) -> Setting:
        """The open ``enum`` the custom reasoning value is written to."""
        top, _, rest = self.custom_reasoning_key.partition(".")
        setting = next((s for s in self.settings if s.key == top), None)
        if setting is None:
            raise KeyError(self.custom_reasoning_key)
        if not rest:
            return setting
        inner = next((f for f in setting.fields if f.key == rest), None)
        if inner is None:
            raise KeyError(self.custom_reasoning_key)
        return inner

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (for the REST surface)."""
        custom = self.custom_reasoning_setting()
        return {
            "wire": self.wire,
            "settings": [setting.to_dict() for setting in self.settings],
            "body_passthrough": self.body_passthrough,
            "custom_reasoning": {
                "key": self.custom_reasoning_key,
                "wire_path": custom.wire_path,
                "suggestions": list(custom.values),
            },
        }


@dataclass(frozen=True)
class SettingProblem:
    """One validation problem: the dotted ``extended`` path and its message."""

    path: str
    message: str


def wire_of(provider_type: str, flavor: str | None) -> str:
    """The wire string for a provider type and (where the type has one) flavor."""
    return provider_type if flavor is None else f"{provider_type}/{flavor}"


def _s(
    key: str,
    stype: SettingType,
    description: str,
    *,
    wire_path: str | None = None,
    values: tuple[Any, ...] = (),
    lo: float | None = None,
    hi: float | None = None,
    fields: tuple[Setting, ...] = (),
    open_: bool = False,
    set_by_reasoning: bool = False,
) -> Setting:
    return Setting(
        key=key,
        type=stype,
        description=description,
        wire_path=wire_path or key,
        values=values,
        min=lo,
        max=hi,
        fields=fields,
        open=open_,
        set_by_reasoning=set_by_reasoning,
    )


# The effort scale on the OpenAI wires; the per-wire rows that pin a smaller
# set (vLLM, ollama) say so in the spec, the ones that do not keep this one.
_EFFORT_OPENAI = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _shared_chat_completions() -> tuple[Setting, ...]:
    """The shared Chat Completions keys: every openchat wire and openrouter."""
    return (
        _s("parallel_tool_calls", "boolean", "Whether the model may call tools in parallel."),
        _s("presence_penalty", "number", "Penalises new tokens according to existing token presence."),
        _s("frequency_penalty", "number", "Penalises tokens that have already appeared in the text."),
        _s("logprobs", "boolean", "Return log probabilities for the generated tokens."),
        _s("top_logprobs", "integer", "Number of most likely tokens and their log probabilities to return."),
        _s("seed", "integer", "Seed for deterministic sampling."),
        _s("user", "string", "Stable end-user identifier sent with the request."),
    )


def _chat_template_kwargs() -> Setting:
    return _s(
        "chat_template_kwargs",
        "object",
        "Extra keyword arguments passed to the chat template.",
        wire_path="body.chat_template_kwargs",
        fields=(_s("enable_thinking", "boolean", "Whether the template should emit thinking."),),
        open_=True,
        set_by_reasoning=True,
    )


def _responses_effort() -> Setting:
    return _s(
        "reasoning_effort",
        "enum",
        "Reasoning effort on the OpenAI scale; any value the model takes passes.",
        values=_EFFORT_OPENAI,
        wire_path="reasoning.effort",
        open_=True,
        set_by_reasoning=True,
    )


def _openresponses_set() -> tuple[Setting, ...]:
    """The openresponses/openai set; lmstudio, vllm and other carry the same keys."""
    return (
        _responses_effort(),
        _s(
            "reasoning_summary",
            "enum",
            "How the reasoning summary is shown.",
            values=("auto", "concise", "detailed"),
            wire_path="reasoning.summary",
        ),
        _s(
            "reasoning_context",
            "enum",
            "Which turns' reasoning items are sent back.",
            values=("auto", "current_turn", "all_turns"),
            wire_path="reasoning.context",
        ),
        _s("reasoning_mode", "enum", "Reasoning mode.", values=("standard", "pro"), wire_path="reasoning.mode"),
        _s(
            "verbosity",
            "enum",
            "Verbosity of the model's text output.",
            values=("low", "medium", "high"),
            wire_path="text.verbosity",
        ),
        _s("service_tier", "string", "Service tier the request may run on."),
        _s("prompt_cache_key", "string", "Key that groups requests that share a prompt cache."),
        _s("prompt_cache_retention", "string", "How long a prompt cache entry is kept."),
        _s("metadata", "object", "Metadata for the response."),
        _s("max_tool_calls", "integer", "Maximum tool calls the model may make."),
        _s("top_logprobs", "integer", "Number of most likely tokens and their log probabilities to return."),
        _s("parallel_tool_calls", "boolean", "Whether the model may call tools in parallel."),
        _s("truncation", "enum", "How the input is truncated.", values=("auto", "disabled")),
        _s("safety_identifier", "string", "Identifier of the user the request is for."),
    )


_CATALOGUES: dict[str, Catalogue] = {
    "openchat/openai": Catalogue(
        wire="openchat/openai",
        settings=(
            _s(
                "reasoning_effort",
                "enum",
                "Reasoning effort on the OpenAI scale; any value the model takes passes.",
                values=_EFFORT_OPENAI,
                open_=True,
                set_by_reasoning=True,
            ),
            _s("verbosity", "enum", "Verbosity of the model's text output.", values=("low", "medium", "high")),
            _s("service_tier", "string", "Service tier the request may run on."),
            _s("prompt_cache_key", "string", "Key that groups requests that share a prompt cache."),
            _s("prompt_cache_retention", "string", "How long a prompt cache entry is kept."),
            _s("safety_identifier", "string", "Identifier of the user the request is for."),
            *_shared_chat_completions(),
        ),
        body_passthrough=False,
        custom_reasoning_key="reasoning_effort",
    ),
    "openchat/vllm": Catalogue(
        wire="openchat/vllm",
        settings=(
            _s(
                "reasoning_effort",
                "enum",
                "Reasoning effort for gpt-oss style templates; any value the model takes passes.",
                values=("low", "medium", "high"),
                open_=True,
                set_by_reasoning=True,
            ),
            _chat_template_kwargs(),
            _s("thinking_token_budget", "integer", "Maximum reasoning tokens the model may use."),
            _s("include_reasoning", "boolean", "Include the reasoning in the reply."),
            _s("top_k", "integer", "Sample from the top k tokens."),
            _s("min_p", "number", "Drop tokens below this fraction of the top token's probability.", lo=0.0, hi=1.0),
            _s("repetition_penalty", "number", "Penalty for repeating existing tokens."),
            _s("min_tokens", "integer", "Minimum tokens to generate before stopping."),
            _s("priority", "integer", "Scheduling priority of the request."),
            *_shared_chat_completions(),
        ),
        body_passthrough=True,
        custom_reasoning_key="reasoning_effort",
    ),
    "openchat/lmstudio": Catalogue(
        wire="openchat/lmstudio",
        settings=(
            _s(
                "reasoning_effort",
                "enum",
                "Reasoning effort (LM Studio 0.4.8+); any value the server takes passes.",
                values=_EFFORT_OPENAI,
                open_=True,
                set_by_reasoning=True,
            ),
            _chat_template_kwargs(),
            _s("reasoning_tokens", "integer", "Maximum reasoning tokens."),
            _s("ttl", "number", "Seconds before an idle model is unloaded."),
            _s("draft_model", "string", "Model used for speculative decoding."),
            _s("top_k", "integer", "Sample from the top k tokens."),
            _s("min_p", "number", "Drop tokens below this fraction of the top token's probability."),
            _s("repeat_penalty", "number", "Penalty for repeating existing tokens."),
            *_shared_chat_completions(),
        ),
        body_passthrough=True,
        custom_reasoning_key="reasoning_effort",
    ),
    "openchat/ollama": Catalogue(
        wire="openchat/ollama",
        settings=(
            _s(
                "reasoning_effort",
                "enum",
                "Reasoning effort for gpt-oss style templates; any value the model takes passes.",
                values=("none", "low", "medium", "high"),
                open_=True,
                set_by_reasoning=True,
            ),
            *_shared_chat_completions(),
        ),
        body_passthrough=True,
        custom_reasoning_key="reasoning_effort",
    ),
    "openchat/other": Catalogue(
        wire="openchat/other",
        settings=(
            _s(
                "reasoning_effort",
                "enum",
                "Reasoning effort; any value the server takes passes.",
                values=_EFFORT_OPENAI,
                open_=True,
                set_by_reasoning=True,
            ),
            _chat_template_kwargs(),
            *_shared_chat_completions(),
        ),
        body_passthrough=True,
        custom_reasoning_key="reasoning_effort",
    ),
    "openchat/strata": Catalogue(
        wire="openchat/strata",
        settings=(
            _s(
                "reasoning_effort",
                "enum",
                "Reasoning effort on Strata's scale (none, low, medium, high); "
                "none turns thinking off and any value the model takes passes.",
                values=("none", "low", "medium", "high"),
                open_=True,
                set_by_reasoning=True,
            ),
            _s("reasoning_budget_tokens", "integer", "Maximum reasoning tokens the model may use.", lo=0),
            _s("top_k", "integer", "Sample from the top k tokens (at most 64).", lo=1, hi=64),
            _s("min_p", "number", "Drop tokens below this fraction of the top token's probability.", lo=0.0, hi=1.0),
            _s("repetition_penalty", "number", "Penalty for repeating existing tokens."),
            _s("penalty_last_n", "integer", "How far back the repetition penalty looks.", lo=0),
            _chat_template_kwargs(),
            _s("return_progress", "boolean", "Stream progress events for the request."),
            *_shared_chat_completions(),
        ),
        body_passthrough=True,
        custom_reasoning_key="reasoning_effort",
    ),
    "openresponses/openai": Catalogue(
        wire="openresponses/openai",
        settings=_openresponses_set(),
        body_passthrough=False,
        custom_reasoning_key="reasoning_effort",
    ),
    "openresponses/lmstudio": Catalogue(
        wire="openresponses/lmstudio",
        settings=_openresponses_set(),
        body_passthrough=True,
        custom_reasoning_key="reasoning_effort",
    ),
    "openresponses/vllm": Catalogue(
        wire="openresponses/vllm",
        settings=_openresponses_set(),
        body_passthrough=True,
        custom_reasoning_key="reasoning_effort",
    ),
    "openresponses/other": Catalogue(
        wire="openresponses/other",
        settings=_openresponses_set(),
        body_passthrough=True,
        custom_reasoning_key="reasoning_effort",
    ),
    "openresponses/strata": Catalogue(
        wire="openresponses/strata",
        settings=(
            _s(
                "reasoning_effort",
                "enum",
                "Reasoning effort on Strata's scale (none, low, medium, high); "
                "none turns thinking off and any value the model takes passes.",
                values=("none", "low", "medium", "high"),
                wire_path="reasoning.effort",
                open_=True,
                set_by_reasoning=True,
            ),
            _s("reasoning_budget_tokens", "integer", "Maximum reasoning tokens the model may use.", lo=0),
            _s("top_k", "integer", "Sample from the top k tokens (at most 64).", lo=1, hi=64),
            _s("min_p", "number", "Drop tokens below this fraction of the top token's probability.", lo=0.0, hi=1.0),
            _s("repetition_penalty", "number", "Penalty for repeating existing tokens."),
            _s("penalty_last_n", "integer", "How far back the repetition penalty looks.", lo=0),
            _s("parallel_tool_calls", "boolean", "Whether the model may call tools in parallel."),
            _s("max_tool_calls", "integer", "Maximum tool calls the model may make."),
            _s("top_logprobs", "integer", "Number of most likely tokens and their log probabilities to return."),
        ),
        body_passthrough=True,
        custom_reasoning_key="reasoning_effort",
    ),
    "anthropic": Catalogue(
        wire="anthropic",
        settings=(
            _s(
                "thinking",
                "object",
                "Extended thinking settings.",
                fields=(
                    _s("type", "enum", "Thinking mode.", values=("adaptive", "enabled", "disabled", "between_tools")),
                    _s("budget_tokens", "integer", "Thinking token budget; required when the type is enabled.", lo=1024),
                    _s("display", "enum", "How the thinking is shown.", values=("summarized", "omitted")),
                ),
                set_by_reasoning=True,
            ),
            _s(
                "output_config",
                "object",
                "Output configuration; carries the effort on Claude 4.6+.",
                fields=(
                    _s(
                        "effort",
                        "enum",
                        "Reasoning effort; any value the model takes passes.",
                        values=("low", "medium", "high", "xhigh", "max"),
                        wire_path="output_config.effort",
                        open_=True,
                    ),
                ),
                open_=True,
                set_by_reasoning=True,
            ),
            _s("top_k", "integer", "Sample from the top k tokens."),
            _s("service_tier", "enum", "Service tier.", values=("auto", "standard_only")),
            _s("metadata", "object", "Metadata for the response."),
            _s("cache_control", "object", "Prompt cache control."),
            _s("speed", "enum", "Inference speed; fast sends the fast-mode beta header.", values=("standard", "fast")),
            _s("inference_geo", "string", "Region the request may run in."),
        ),
        body_passthrough=False,
        custom_reasoning_key="output_config.effort",
    ),
    "gemini": Catalogue(
        wire="gemini",
        settings=(
            _s(
                "thinking_level",
                "enum",
                "Thinking level; any level the model takes passes.",
                values=("MINIMAL", "LOW", "MEDIUM", "HIGH"),
                wire_path="thinking_config.thinking_level",
                open_=True,
                set_by_reasoning=True,
            ),
            _s(
                "thinking_budget",
                "integer",
                "Thinking token budget.",
                wire_path="thinking_config.thinking_budget",
                set_by_reasoning=True,
            ),
            _s(
                "include_thoughts",
                "boolean",
                "Include the thoughts in the response.",
                wire_path="thinking_config.include_thoughts",
            ),
            _s("top_k", "integer", "Sample from the top k tokens."),
            _s("seed", "integer", "Seed for deterministic sampling."),
            _s("presence_penalty", "number", "Penalises new tokens according to existing token presence."),
            _s("frequency_penalty", "number", "Penalises tokens that have already appeared in the text."),
            _s("safety_settings", "object", "Safety settings per harm category."),
            _s("logprobs", "integer", "Number of most likely tokens to return log probabilities for."),
            _s("response_logprobs", "boolean", "Include log probabilities in the response."),
            _s("media_resolution", "string", "Resolution of the generated images."),
            _s("service_tier", "string", "Service tier the request may run on."),
        ),
        body_passthrough=False,
        custom_reasoning_key="thinking_level",
    ),
    "ollama": Catalogue(
        wire="ollama",
        settings=(
            _s(
                "think",
                "enum",
                "Whether or how much the model thinks; any value the model takes passes.",
                values=(True, False, "low", "medium", "high"),
                open_=True,
                set_by_reasoning=True,
            ),
            _s("keep_alive", "string", "How long the model stays loaded."),
            _s("top_k", "integer", "Sample from the top k tokens.", wire_path="options.top_k"),
            _s("seed", "integer", "Seed for deterministic sampling.", wire_path="options.seed"),
            _s("repeat_penalty", "number", "Penalty for repeating existing tokens.", wire_path="options.repeat_penalty"),
            _s(
                "frequency_penalty",
                "number",
                "Penalises tokens that have already appeared in the text.",
                wire_path="options.frequency_penalty",
            ),
            _s(
                "presence_penalty",
                "number",
                "Penalises new tokens according to existing token presence.",
                wire_path="options.presence_penalty",
            ),
            _s("mirostat", "integer", "Mirostat sampling mode.", wire_path="options.mirostat"),
            _s("mirostat_tau", "number", "Mirostat target loss.", wire_path="options.mirostat_tau"),
            _s("mirostat_eta", "number", "Mirostat learning rate.", wire_path="options.mirostat_eta"),
            _s("tfs_z", "number", "Tail free sampling.", wire_path="options.tfs_z"),
            _s("typical_p", "number", "Typical probability sampling.", wire_path="options.typical_p"),
            _s("num_ctx", "integer", "Context window size.", wire_path="options.num_ctx"),
            _s("num_batch", "integer", "Batch size.", wire_path="options.num_batch"),
            _s("num_gpu", "integer", "Number of GPU layers to load.", wire_path="options.num_gpu"),
            _s("min_p", "number", "Drop tokens below this fraction of the top token's probability."),
            _s("repeat_last_n", "integer", "How far back the repeat penalty looks."),
        ),
        body_passthrough=False,
        custom_reasoning_key="think",
    ),
    "openrouter": Catalogue(
        wire="openrouter",
        settings=(
            _s(
                "reasoning",
                "object",
                "Reasoning settings; effort and max_tokens, not both.",
                fields=(
                    _s(
                        "effort",
                        "enum",
                        "Reasoning effort; any value the upstream model takes passes.",
                        values=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
                        wire_path="reasoning.effort",
                        open_=True,
                    ),
                    _s("max_tokens", "integer", "Maximum reasoning tokens.", wire_path="reasoning.max_tokens"),
                    _s(
                        "exclude",
                        "boolean",
                        "The model reasons but the reasoning is not returned (include_reasoning is its deprecated alias).",
                        wire_path="reasoning.exclude",
                    ),
                    _s("enabled", "boolean", "Whether reasoning is on.", wire_path="reasoning.enabled"),
                ),
                set_by_reasoning=True,
            ),
            _s("verbosity", "enum", "Verbosity of the model's text output.", values=("low", "medium", "high")),
            _s("provider", "object", "Provider routing preferences.", open_=True),
            _s("top_k", "integer", "Sample from the top k tokens."),
            _s("min_p", "number", "Drop tokens below this fraction of the top token's probability."),
            _s("top_a", "number", "Sample from the smallest set of tokens above this total probability."),
            _s("repetition_penalty", "number", "Penalty for repeating existing tokens."),
            *_shared_chat_completions(),
        ),
        body_passthrough=True,
        custom_reasoning_key="reasoning.effort",
    ),
    "codex": Catalogue(
        wire="codex", settings=(_responses_effort(),), body_passthrough=False,
        custom_reasoning_key="reasoning_effort",
    ),
    "claude_code": Catalogue(
        wire="claude_code",
        settings=(
            _s(
                "effort",
                "enum",
                "Reasoning effort for the CLI --effort flag; any effort the model takes passes.",
                values=("low", "medium", "high", "xhigh", "max"),
                open_=True,
                set_by_reasoning=True,
            ),
        ),
        body_passthrough=False,
        custom_reasoning_key="effort",
    ),
}


def catalogue_for(wire: str) -> Catalogue:
    """The catalogue of one wire (KeyError for a wire this module does not know)."""
    return _CATALOGUES[wire]


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _in_values(value: Any, values: tuple[Any, ...]) -> bool:
    """Membership compared with type: True is not "true", a bool is never an integer or number."""
    for candidate in values:
        if type(value) is type(candidate) and value == candidate:
            return True
    return False


def _range_problem(setting: Setting, value: float, path: str) -> SettingProblem | None:
    if setting.min is not None and value < setting.min:
        return SettingProblem(path, f"extended.{path}: below minimum {setting.min:g}")
    if setting.max is not None and value > setting.max:
        return SettingProblem(path, f"extended.{path}: above maximum {setting.max:g}")
    return None


def _validate_value(setting: Setting, value: Any, path: str, problems: list[SettingProblem]) -> None:
    if setting.type == "enum":
        if setting.open:
            if _in_values(value, setting.values) or (isinstance(value, str) and value):
                return
            problems.append(
                SettingProblem(
                    path,
                    f"extended.{path}: must be a non-empty string or one of: {', '.join(_fmt_value(v) for v in setting.values)}",
                )
            )
        else:
            if _in_values(value, setting.values):
                return
            problems.append(
                SettingProblem(
                    path,
                    f"extended.{path}: not one of: {', '.join(_fmt_value(v) for v in setting.values)}",
                )
            )
    elif setting.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(SettingProblem(path, f"extended.{path}: expected an integer"))
            return
        problem = _range_problem(setting, value, path)
        if problem is not None:
            problems.append(problem)
    elif setting.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(SettingProblem(path, f"extended.{path}: expected a number"))
            return
        problem = _range_problem(setting, value, path)
        if problem is not None:
            problems.append(problem)
    elif setting.type == "boolean":
        if not isinstance(value, bool):
            problems.append(SettingProblem(path, f"extended.{path}: expected a boolean"))
    elif setting.type == "string":
        if not isinstance(value, str):
            problems.append(SettingProblem(path, f"extended.{path}: expected a string"))
    elif setting.type == "object":
        if not isinstance(value, dict):
            problems.append(SettingProblem(path, f"extended.{path}: expected an object"))
            return
        by_key = {field.key: field for field in setting.fields}
        for inner, inner_value in value.items():
            field = by_key.get(inner)
            if field is not None:
                _validate_value(field, inner_value, f"{path}.{inner}", problems)
            elif setting.fields and not setting.open:
                # A closed object with known fields refuses the rest; an object
                # with no fields is opaque and takes any inner shape.
                problems.append(
                    SettingProblem(
                        f"{path}.{inner}",
                        f"extended.{path}.{inner} is not a field of {setting.key}; accepted: {', '.join(sorted(by_key))}",
                    )
                )


def _validate_body(catalogue: Catalogue, body: Any, problems: list[SettingProblem]) -> None:
    if not isinstance(body, dict):
        problems.append(SettingProblem("body", "extended.body: expected an object"))
        return
    keys = catalogue.keys()
    for inner, _inner_value in body.items():
        if inner in RESERVED_BODY_FIELDS:
            problems.append(
                SettingProblem(f"body.{inner}", f"extended.body.{inner}: reserved field, primer sets it itself")
            )
        elif inner in keys:
            problems.append(
                SettingProblem(f"body.{inner}", f"extended.body.{inner}: repeats the {inner} setting; set it at the top level instead")
            )
        # Anything else passes: body carries server fields the catalogue does not name.


def validate_extended(wire: str, extended: Mapping[str, Any]) -> list[SettingProblem]:
    """Validate one ``extended`` object against the wire's catalogue (empty = valid)."""
    catalogue = catalogue_for(wire)
    problems: list[SettingProblem] = []
    settings = {setting.key: setting for setting in catalogue.settings}
    for key, value in extended.items():
        if key in settings:
            _validate_value(settings[key], value, key, problems)
        elif key == "body" and catalogue.body_passthrough:
            _validate_body(catalogue, value, problems)
        else:
            accepted = sorted(catalogue.keys() | ({"body"} if catalogue.body_passthrough else frozenset()))
            problems.append(
                SettingProblem(
                    key,
                    f"extended.{key} is not a setting for {wire}; accepted: {', '.join(accepted)}",
                )
            )
    # Wire-specific cross-field rule (spec section 5): openrouter's reasoning
    # takes effort or max_tokens, not both.
    if wire == "openrouter" and isinstance(extended.get("reasoning"), dict):
        reasoning = extended["reasoning"]
        if reasoning.get("effort") is not None and reasoning.get("max_tokens") is not None:
            problems.append(SettingProblem("reasoning", "extended.reasoning: effort and max_tokens are not both allowed"))
    return problems


# One WARNING per process per (provider_id, key), so a flavor change that
# drops a key does not spam every call (spec section 6).
_warned_dropped: set[tuple[str, str]] = set()


def warn_dropped(adapter: str, provider_id: str, keys: Iterable[str]) -> None:
    """Log the extended keys the wire no longer takes, once per (provider, key).

    Names the keys, never their values.
    """
    for key in keys:
        if (provider_id, key) in _warned_dropped:
            continue
        _warned_dropped.add((provider_id, key))
        logger.warning(
            "dropping extended key %s for provider %s (adapter %s): the wire no longer takes it; "
            "remove it from the profile's extended settings",
            key,
            provider_id,
            adapter,
        )
