"""OpenResponses LLM adapter — wraps the OpenAI Responses API.

Subclasses :class:`primer.int.LLM` and translates the universal chat
interface (:mod:`primer.model.chat`) onto the OpenAI Responses wire
format. Supports both real OpenAI and LM Studio's OpenAI-compatible
endpoint via the :class:`OpenResponsesFlavor` discriminator on the
provider config.

See the design spec at
``docs/superpowers/specs/2026-04-26-openresponses-llm-adapter-design.md``
for the per-event mapping table, exception classification, and flavor
policy details.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel

from primer.common.openai_errors import classify_openai_exception
from primer.int.llm import LLM
from primer.llm._openai_common import build_sampling_params as _build_sampling_params_impl
from primer.model.except_ import (
    ConfigError,
    ModelNotFoundError,
    UnsupportedContentError,
)
from primer.model.chat import (
    AudioPart,
    Citation,
    DocumentPart,
    Done,
    Error as ChatError,
    ExtendedEvent,
    ExtendedPart,
    ImagePart,
    MediaDelta,
    Message,
    Part,
    RawReasoningDelta,
    ReasoningDelta,
    RefusalDelta,
    ServerToolCallDelta,
    ServerToolCallEnd,
    ServerToolCallStart,
    StopReason,
    StreamEvent,
    StreamStart,
    TextDelta,
    TextPart,
    Tool,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolChoice,
    ToolResultPart,
    Usage,
    VideoPart,
)
from primer.int.coordinator import RateLimiter
from primer.model.provider import (
    LLMProvider,
    LLMProviderType,
    OpenResponsesConfig,
    OpenResponsesFlavor,
)
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Input mapping: primer.model.chat -> OpenAI Responses input items            #
# --------------------------------------------------------------------------- #


_OPENAI_AUDIO_FORMATS: dict[str, str] = {
    "audio/mp3": "mp3",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
}


def _part_to_input_content(part: Part, *, role: str = "user") -> dict[str, Any]:
    """Translate one universal :class:`Part` into one OpenAI input
    content dict.

    Pure function, no I/O. Raises :class:`UnsupportedContentError` for
    parts the OpenAI Responses API does not accept.

    ``role`` controls the text content discriminator: assistant messages
    in the Responses API expect ``output_text`` while user/system use
    ``input_text``. Passing the role lets us replay assistant turns
    from history without the server returning ``invalid_union``.
    """
    if isinstance(part, TextPart):
        text_type = "output_text" if role == "assistant" else "input_text"
        return {"type": text_type, "text": part.text}

    if isinstance(part, ImagePart):
        out: dict[str, Any] = {"type": "input_image"}
        if part.data is not None:
            mime = part.mime_type or "application/octet-stream"
            out["image_url"] = (
                f"data:{mime};base64,{base64.b64encode(part.data).decode()}"
            )
        elif part.url is not None:
            out["image_url"] = part.url
        else:  # file_id (validator guarantees one of the three is present)
            out["file_id"] = part.file_id
        if part.detail is not None:
            out["detail"] = part.detail
        return out

    if isinstance(part, DocumentPart):
        out = {"type": "input_file"}
        if part.data is not None:
            # OpenAI's Responses API accepts inline file bytes via
            # ``file_data`` formatted as a data URI (e.g.
            # ``data:application/pdf;base64,JVBER...``). The Stainless-
            # generated SDK docstring labels this "base64-encoded data
            # of the file" which sounds like raw base64, but in practice
            # OpenAI's server rejects raw base64 with ``invalid_union``
            # on the ``input`` parameter. Production OSS impls
            # (home-assistant, agno, OpenBMB/ChatDev) all use the data
            # URI format here — keep us aligned.
            mime = part.mime_type or "application/pdf"
            b64 = base64.b64encode(part.data).decode()
            out["file_data"] = f"data:{mime};base64,{b64}"
            out["filename"] = part.filename or "file"
        elif part.url is not None:
            out["file_url"] = part.url
            if part.filename is not None:
                out["filename"] = part.filename
        else:
            out["file_id"] = part.file_id
            if part.filename is not None:
                out["filename"] = part.filename
        return out

    if isinstance(part, ExtendedPart):
        ext = part.extended
        if isinstance(ext, AudioPart):
            if ext.data is None:
                raise UnsupportedContentError(
                    "OpenAI Responses requires inline base64 audio; "
                    "pre-fetch URL to bytes"
                )
            fmt = _OPENAI_AUDIO_FORMATS.get(ext.mime_type or "")
            if fmt is None:
                raise UnsupportedContentError(
                    f"OpenAI Responses accepts only audio/mp3, audio/mpeg, "
                    f"or audio/wav; got mime_type={ext.mime_type!r}"
                )
            return {
                "type": "input_audio",
                "input_audio": {
                    "data": base64.b64encode(ext.data).decode(),
                    "format": fmt,
                },
            }
        if isinstance(ext, VideoPart):
            raise UnsupportedContentError(
                "OpenAI Responses does not accept video input"
            )
        raise UnsupportedContentError(
            f"OpenAI Responses does not support extended part type {ext.type!r}"
        )

    # ToolCallPart / ToolResultPart should never arrive here — they're
    # routed by _messages_to_input_items at the message walker level.
    raise UnsupportedContentError(  # pragma: no cover
        f"unexpected part type {type(part).__name__}"
    )


def _messages_to_input_items(messages: list[Message]) -> list[dict[str, Any]]:
    """Walk a chat history and produce an OpenAI Responses ``input`` list.

    System messages stay inline as ``role="system"`` items. Assistant
    tool calls split out into top-level ``function_call`` items
    (flushing any in-progress message). Tool-role messages flatten to
    one ``function_call_output`` per :class:`ToolResultPart`.
    """
    items: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "tool":
            for part in msg.parts:
                if not isinstance(part, ToolResultPart):
                    raise UnsupportedContentError(
                        f"tool-role messages must contain only ToolResultPart; "
                        f"got {type(part).__name__}"
                    )
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": part.id,
                        "output": part.output,
                    }
                )
            continue

        current: dict[str, Any] | None = None
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                if current is not None and current["content"]:
                    items.append(current)
                    current = None
                items.append(
                    {
                        "type": "function_call",
                        "call_id": part.id,
                        "name": part.name,
                        "arguments": json.dumps(part.arguments),
                    }
                )
            elif isinstance(part, ToolResultPart):
                raise UnsupportedContentError(
                    "ToolResultPart is only valid inside a tool-role message"
                )
            else:
                if current is None:
                    current = {"role": msg.role, "content": []}
                current["content"].append(
                    _part_to_input_content(part, role=msg.role),
                )
        if current is not None and current["content"]:
            items.append(current)

    return items


# --------------------------------------------------------------------------- #
# Tool / tool-choice / response-format translators                            #
# --------------------------------------------------------------------------- #


def _tool_to_openai(tool: Tool) -> dict[str, Any]:
    """Translate a universal :class:`Tool` into one OpenAI function-tool dict.

    ``tool.toolset_id`` is caller-side correlation only and is not
    transmitted. ``strict`` is omitted; OpenAI defaults apply.
    """
    return {
        "type": "function",
        "name": tool.id,
        "description": tool.description,
        "parameters": tool.args_schema,
    }


def _tool_choice_to_openai(choice: ToolChoice | None) -> Any:
    """Translate the universal :data:`ToolChoice` to the OpenAI value.

    Returns ``None`` to signal "do not include in the request"; the
    caller must drop the key from the payload.
    """
    if choice is None:
        return None
    if choice in ("auto", "required", "none"):
        return choice
    return {"type": "function", "name": choice}


def _response_format_to_text_param(
    fmt: type[BaseModel] | dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Translate ``response_format`` into the ``text=`` parameter dict.

    Returns ``None`` to signal "do not include"; the caller drops the
    key. Accepts either a Pydantic class (uses ``.model_json_schema()``
    + class name) or a raw JSON Schema dict (uses ``"schema"`` as the
    name).
    """
    if fmt is None:
        return None
    if isinstance(fmt, dict):
        schema = fmt
        name = "schema"
    elif isinstance(fmt, type) and issubclass(fmt, BaseModel):
        schema = fmt.model_json_schema()
        name = fmt.__name__
    else:
        raise ConfigError(
            f"response_format must be a Pydantic class or dict; "
            f"got {type(fmt).__name__}"
        )
    return {
        "format": {
            "type": "json_schema",
            "name": name,
            "schema": schema,
            "strict": True,
        }
    }


# --------------------------------------------------------------------------- #
# Sampling and extended-kwarg extraction                                       #
# --------------------------------------------------------------------------- #


def _build_sampling_params(
    *,
    temperature: float | None,
    top_p: float | None,
    max_output_tokens: int | None,
    stop: list[str] | None,
) -> dict[str, Any]:
    """Forward sampling knobs to the OpenAI Responses wire format.

    Delegates to :func:`primer.llm._openai_common.build_sampling_params`
    with ``target="responses"``. Kept as a module-local function so the
    existing test surface (``primer.llm.openresponses._build_sampling_params``)
    stays stable.
    """
    return _build_sampling_params_impl(
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        stop=stop,
        target="responses",
    )


_RECOGNISED_EXTENDED_PASSTHROUGH: frozenset[str] = frozenset({
    "parallel_tool_calls",
    "prompt_cache_key",
    "service_tier",
    "metadata",
    "max_tool_calls",
    "top_logprobs",
})


def _extract_extended_kwargs(extended: dict[str, Any] | None) -> dict[str, Any]:
    """Project the universal ``extended`` dict onto OpenAI Responses kwargs.

    Recognised keys are forwarded; unknown keys are dropped with a
    single DEBUG log line listing them so users diagnosing a "my knob
    isn't taking effect" issue can see the reason.
    """
    if not extended:
        return {}

    out: dict[str, Any] = {}
    reasoning: dict[str, Any] = {}
    dropped: list[str] = []

    for key, value in extended.items():
        if key == "reasoning_effort":
            reasoning["effort"] = value
        elif key == "reasoning_summary":
            reasoning["summary"] = value
        elif key in _RECOGNISED_EXTENDED_PASSTHROUGH:
            out[key] = value
        else:
            dropped.append(key)

    if reasoning:
        out["reasoning"] = reasoning
    if dropped:
        logger.debug(
            "OpenResponses adapter dropped unknown extended kwargs: %s",
            ", ".join(sorted(dropped)),
        )
    return out


# --------------------------------------------------------------------------- #
# Stream event translation                                                     #
# --------------------------------------------------------------------------- #


_SERVER_TOOL_ITEM_TYPES: dict[str, str] = {
    "web_search_call": "web_search",
    "file_search_call": "file_search",
    "code_interpreter_call": "code_interpreter",
    "image_generation_call": "image_generation",
    "mcp_call": "mcp",
}


@dataclass
class _StreamState:
    """Per-stream mutable state used by :func:`_translate_event`.

    A new instance is created at the top of every ``stream()`` call.
    """

    next_index: int = 0
    block_index: dict[tuple[str, int | None], int] = field(default_factory=dict)
    item_kind: dict[str, str] = field(default_factory=dict)
    item_call_id: dict[str, str] = field(default_factory=dict)
    item_call_name: dict[str, str] = field(default_factory=dict)
    saw_function_call: bool = False


def _resolve_index(
    state: _StreamState, item_id: str, content_index: int | None = None
) -> int:
    """Look up or assign the flat block index for an (item_id, content_index)."""
    key = (item_id, content_index)
    if key not in state.block_index:
        state.block_index[key] = state.next_index
        state.next_index += 1
    return state.block_index[key]


def _map_stop_reason(status: str, state: _StreamState) -> StopReason:
    if status == "completed":
        return "tool_use" if state.saw_function_call else "stop"
    if status == "failed":
        return "error"
    return "other"


def _map_incomplete_reason(reason: str | None) -> StopReason:
    if reason == "max_output_tokens":
        return "max_tokens"
    if reason == "content_filter":
        return "content_filter"
    return "other"


def _build_usage(usage_obj: Any) -> Usage | None:
    """Translate a Responses ``usage`` object to a :class:`Usage` event.

    Returns ``None`` if the object is missing or doesn't expose token
    counts (defensive — some endpoints omit it).
    """
    if usage_obj is None:
        return None
    input_tokens = getattr(usage_obj, "input_tokens", None)
    output_tokens = getattr(usage_obj, "output_tokens", None)
    if input_tokens is None or output_tokens is None:
        return None
    cached: int | None = None
    reasoning: int | None = None
    details_in = getattr(usage_obj, "input_tokens_details", None)
    if details_in is not None:
        cached = getattr(details_in, "cached_tokens", None)
    details_out = getattr(usage_obj, "output_tokens_details", None)
    if details_out is not None:
        reasoning = getattr(details_out, "reasoning_tokens", None)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
        cumulative=False,
    )


def _annotation_to_citation(annotation: Any, index: int) -> Citation:
    """Translate an OpenAI annotation event to a :class:`Citation`.

    The annotation surface has three documented shapes — ``url_citation``,
    ``file_citation``, ``container_file_citation`` — plus possible future
    types. We populate whatever fields are present and leave the rest
    as ``None``.
    """
    return Citation(
        source_url=getattr(annotation, "url", None),
        source_title=getattr(annotation, "title", None),
        source_id=getattr(annotation, "file_id", None)
        or getattr(annotation, "container_id", None),
        quoted_text=getattr(annotation, "quote", None)
        or getattr(annotation, "text", None),
        start_index=getattr(annotation, "start_index", None),
        end_index=getattr(annotation, "end_index", None),
        index=index,
    )


def _translate_event(  # noqa: C901  (intentional dispatch table)
    event: Any, state: _StreamState
) -> list[StreamEvent]:
    """Translate one OpenAI Responses streaming event into universal events.

    Pure function. Returns zero or more :data:`StreamEvent`s the
    adapter should yield. Mutates ``state`` to track block indices and
    function-call observations.
    """
    etype = getattr(event, "type", "")

    if etype == "response.created":
        response = getattr(event, "response", None)
        return [
            StreamStart(
                request_id=getattr(response, "id", None),
                model=getattr(response, "model", "") or "",
            )
        ]

    if etype == "response.output_item.added":
        item = getattr(event, "item", None)
        item_type = getattr(item, "type", "")
        item_id = getattr(item, "id", "") or ""

        if item_type in {"message", "reasoning"}:
            _resolve_index(state, item_id, None)
            state.item_kind[item_id] = item_type
            return []

        if item_type == "function_call":
            idx = _resolve_index(state, item_id, None)
            state.saw_function_call = True
            state.item_kind[item_id] = item_type
            call_id = getattr(item, "call_id", "") or ""
            name = getattr(item, "name", "") or ""
            state.item_call_id[item_id] = call_id
            state.item_call_name[item_id] = name
            return [ToolCallStart(id=call_id, name=name, index=idx)]

        if item_type in _SERVER_TOOL_ITEM_TYPES:
            idx = _resolve_index(state, item_id, None)
            state.item_kind[item_id] = item_type
            return [
                ExtendedEvent(
                    extended=ServerToolCallStart(
                        id=item_id,
                        tool_name=_SERVER_TOOL_ITEM_TYPES[item_type],
                        index=idx,
                    )
                )
            ]

        # Unknown output_item type — register and continue.
        _resolve_index(state, item_id, None)
        state.item_kind[item_id] = item_type
        return []

    if etype == "response.content_part.added":
        item_id = getattr(event, "item_id", "") or ""
        ci = getattr(event, "content_index", 0)
        _resolve_index(state, item_id, ci)
        return []

    if etype == "response.output_text.delta":
        item_id = getattr(event, "item_id", "") or ""
        ci = getattr(event, "content_index", 0)
        idx = _resolve_index(state, item_id, ci)
        return [TextDelta(text=getattr(event, "delta", "") or "", index=idx)]

    if etype == "response.reasoning_summary_text.delta":
        item_id = getattr(event, "item_id", "") or ""
        ci = getattr(event, "summary_index", getattr(event, "content_index", 0))
        idx = _resolve_index(state, item_id, ci)
        return [ReasoningDelta(text=getattr(event, "delta", "") or "", index=idx)]

    if etype == "response.reasoning_text.delta":
        item_id = getattr(event, "item_id", "") or ""
        ci = getattr(event, "content_index", 0)
        idx = _resolve_index(state, item_id, ci)
        return [
            ExtendedEvent(
                extended=RawReasoningDelta(
                    text=getattr(event, "delta", "") or "", index=idx
                )
            )
        ]

    if etype == "response.refusal.delta":
        item_id = getattr(event, "item_id", "") or ""
        ci = getattr(event, "content_index", 0)
        idx = _resolve_index(state, item_id, ci)
        return [
            ExtendedEvent(
                extended=RefusalDelta(
                    text=getattr(event, "delta", "") or "", index=idx
                )
            )
        ]

    if etype == "response.function_call_arguments.delta":
        item_id = getattr(event, "item_id", "") or ""
        idx = _resolve_index(state, item_id, None)
        call_id = state.item_call_id.get(item_id, "")
        return [
            ToolCallDelta(
                id=call_id,
                arguments_delta=getattr(event, "delta", "") or "",
                index=idx,
            )
        ]

    if etype == "response.function_call_arguments.done":
        item_id = getattr(event, "item_id", "") or ""
        idx = _resolve_index(state, item_id, None)
        call_id = state.item_call_id.get(item_id, "")
        raw_args = getattr(event, "arguments", "") or "{}"
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed = {}
        return [ToolCallEnd(id=call_id, arguments=parsed, index=idx)]

    if etype == "response.audio.delta":
        item_id = getattr(event, "item_id", "") or ""
        ci = getattr(event, "content_index", 0)
        idx = _resolve_index(state, item_id, ci)
        raw = getattr(event, "delta", "") or ""
        return [
            MediaDelta(
                kind="audio",
                data=base64.b64decode(raw),
                mime_type="audio/mpeg",
                index=idx,
            )
        ]

    if etype == "response.image_generation_call.partial_image":
        item_id = getattr(event, "item_id", "") or ""
        idx = _resolve_index(state, item_id, None)
        raw = getattr(event, "partial_image_b64", "") or ""
        return [
            MediaDelta(
                kind="image",
                data=base64.b64decode(raw),
                mime_type="image/png",
                index=idx,
            )
        ]

    if etype == "response.code_interpreter_call.code.delta":
        item_id = getattr(event, "item_id", "") or ""
        idx = _resolve_index(state, item_id, None)
        return [
            ExtendedEvent(
                extended=ServerToolCallDelta(
                    id=item_id, text=getattr(event, "delta", None), index=idx
                )
            )
        ]

    if etype == "response.output_text_annotation.added":
        item_id = getattr(event, "item_id", "") or ""
        ci = getattr(event, "content_index", 0)
        idx = _resolve_index(state, item_id, ci)
        annotation = getattr(event, "annotation", None)
        if annotation is None:
            return []
        return [ExtendedEvent(extended=_annotation_to_citation(annotation, idx))]

    if etype == "response.completed":
        out: list[StreamEvent] = []
        response = getattr(event, "response", None)
        usage_obj = getattr(response, "usage", None) if response is not None else None
        usage_event = _build_usage(usage_obj)
        if usage_event is not None:
            out.append(usage_event)
        out.append(
            Done(
                stop_reason=_map_stop_reason("completed", state),
                raw_reason="completed",
            )
        )
        return out

    if etype == "response.failed":
        return [Done(stop_reason="error", raw_reason="failed")]

    if etype == "response.incomplete":
        response = getattr(event, "response", None)
        details = getattr(response, "incomplete_details", None) if response else None
        reason = getattr(details, "reason", None)
        return [
            Done(
                stop_reason=_map_incomplete_reason(reason),
                raw_reason=f"incomplete:{reason}" if reason else "incomplete",
            )
        ]

    if etype == "error":
        return [
            ChatError(
                fatal=False,
                code=getattr(event, "code", None),
                message=getattr(event, "message", "") or "unknown error",
            )
        ]

    if etype.endswith(".completed") or etype.endswith(".done"):
        # A generic server-tool completion. Skip events we already handled
        # explicitly (function_call_arguments.done, refusal.done, etc.).
        if etype in {
            "response.function_call_arguments.done",
            "response.refusal.done",
            "response.output_text.done",
            "response.audio.done",
            "response.audio_transcript.done",
            "response.completed",
        }:
            return []
        item_id = getattr(event, "item_id", "") or ""
        if item_id and state.item_kind.get(item_id) in _SERVER_TOOL_ITEM_TYPES:
            idx = state.block_index.get((item_id, None), 0)
            return [
                ExtendedEvent(
                    extended=ServerToolCallEnd(id=item_id, result=None, index=idx)
                )
            ]
        return []

    # Unhandled event type — DEBUG and skip.
    logger.debug("OpenResponses adapter ignoring event type: %s", etype)
    return []


# --------------------------------------------------------------------------- #
# Flavor policy                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _FlavorPolicy:
    """Per-flavor behavioural knobs for the OpenResponses adapter.

    Resolved once at construction time from
    :class:`OpenResponsesConfig.flavor` and consulted at the divergence
    sites in the adapter. New flavors land as additional dict entries
    in :data:`_POLICY_BY_FLAVOR`.

    Attributes
    ----------
    require_api_key
        When True, an empty ``api_key`` raises :class:`ConfigError` in
        ``__init__``.
    drop_encrypted_reasoning
        When True, strip ``encrypted_content`` from any reasoning items
        appearing in input messages before sending. Defensive: the
        default ``output_to_message`` converter doesn't preserve
        reasoning, so this is forward-compat scaffolding today.
    expect_reasoning_under_store_true
        Informational only with ``store=False`` hardcoded. Documented
        for future readers in case ``store=True`` is later supported.
    """

    require_api_key: bool
    drop_encrypted_reasoning: bool
    expect_reasoning_under_store_true: bool


_POLICY_BY_FLAVOR: dict[OpenResponsesFlavor, _FlavorPolicy] = {
    OpenResponsesFlavor.OPENAI: _FlavorPolicy(
        require_api_key=True,
        drop_encrypted_reasoning=False,
        expect_reasoning_under_store_true=True,
    ),
    OpenResponsesFlavor.LMSTUDIO: _FlavorPolicy(
        require_api_key=False,
        drop_encrypted_reasoning=True,
        expect_reasoning_under_store_true=False,
    ),
    OpenResponsesFlavor.OTHER: _FlavorPolicy(
        require_api_key=True,
        drop_encrypted_reasoning=False,
        expect_reasoning_under_store_true=True,
    ),
}


# --------------------------------------------------------------------------- #
# Adapter                                                                      #
# --------------------------------------------------------------------------- #


from primer.llm._trace import _serialize_messages  # noqa: E402


class OpenResponsesLLM(LLM):
    """Streaming LLM adapter for the OpenAI Responses API."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        rate_limiter: RateLimiter | None = None,
        trace_llm_io: bool = False,
    ) -> None:
        if provider.provider != LLMProviderType.OPENRESPONSES:
            raise ConfigError(
                f"OpenResponsesLLM requires provider type OPENRESPONSES; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, OpenResponsesConfig):
            raise ConfigError(
                "OpenResponsesLLM requires OpenResponsesConfig in provider.config"
            )

        self._provider = provider
        self._config: OpenResponsesConfig = provider.config
        self._policy = _POLICY_BY_FLAVOR[provider.config.flavor]

        # The flavor policy decides whether a key is required up-front;
        # the Pydantic config allows api_key=None so unauthenticated
        # endpoints can be registered. For flavors that need a key
        # (OPENAI, OTHER), we still fail fast at adapter construction
        # rather than letting the upstream 401 surface later.
        key_present = (
            provider.config.api_key is not None
            and bool(provider.config.api_key.get_secret_value())
        )
        if self._policy.require_api_key and not key_present:
            raise ConfigError(
                f"api_key is required for flavor={provider.config.flavor.value}"
            )

        self._client: AsyncOpenAI | None = None
        if rate_limiter is None:
            from primer.coordinator.in_memory import InMemoryRateLimiter
            rate_limiter = InMemoryRateLimiter()
        self._rate_limiter = rate_limiter
        self._rate_limit_key = f"llm:{provider.id}"
        self._max_concurrency = provider.limits.max_concurrency
        self._trace_llm_io = trace_llm_io

        logger.info(
            "OpenResponses adapter initialized",
            extra={
                "provider_id": provider.id,
                "flavor": provider.config.flavor.value,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
            },
        )

    async def list_models(self) -> Iterable[str]:
        return [m.name for m in self._provider.models]

    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[Tool] | None = None,
    ) -> int:
        """Delegate to ``primer.llm._tokenizer.openai`` (tiktoken)."""
        from primer.llm._tokenizer.openai import count_tokens_openai
        return count_tokens_openai(model=model, messages=messages, tools=tools)

    def _get_client(self) -> AsyncOpenAI:
        """Construct the AsyncOpenAI client lazily on first use."""
        if self._client is None:
            # AsyncOpenAI rejects api_key=None outright (it raises),
            # so for unauthenticated endpoints we pass a sentinel
            # empty-string placeholder. LM Studio / vLLM ignore the
            # Authorization header content; real OpenAI returns 401
            # which is the intended surface for misconfiguration.
            key = (
                self._config.api_key.get_secret_value()
                if self._config.api_key is not None
                else ""
            ) or "no-key-required"
            self._client = AsyncOpenAI(
                base_url=str(self._config.url),
                api_key=key,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the openai SDK client (releases the httpx pool).
        Idempotent (safe to call twice)."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def stream(  # type: ignore[override]
        self,
        *,
        model: str,
        messages: list[Message],
        temperature: float | None = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        stop: list[str] | None = None,
        response_format: type[BaseModel] | dict[str, Any] | None = None,
        tools: list[Tool] | None = None,
        tool_choice: ToolChoice | None = None,
        extended: dict[str, Any] | None = None,
    ):
        allowed = {m.name for m in self._provider.models}
        if model not in allowed:
            raise ModelNotFoundError(
                f"model {model!r} is not configured for provider "
                f"{self._provider.id!r}; configured models: {sorted(allowed)}"
            )

        request: dict[str, Any] = {
            "model": model,
            "input": _messages_to_input_items(messages),
            "store": False,
            "stream": True,
        }
        request.update(
            _build_sampling_params(
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
                stop=stop,
            )
        )
        if tools:
            request["tools"] = [_tool_to_openai(t) for t in tools]
        choice_value = _tool_choice_to_openai(tool_choice)
        if choice_value is not None:
            request["tool_choice"] = choice_value
        text_param = _response_format_to_text_param(response_format)
        if text_param is not None:
            request["text"] = text_param
        request.update(_extract_extended_kwargs(extended))

        logger.info(
            "OpenResponses stream starting",
            extra={
                "provider_id": self._provider.id,
                "model": model,
                "message_count": len(messages),
                "tool_count": len(tools) if tools else 0,
            },
        )

        import time
        _provider_kind = self._provider.provider.value
        _tracer = _tracing.get_tracer("primer.llm")
        _t0 = time.monotonic()
        with _tracer.start_as_current_span("llm.stream") as _span:
            _span.set_attribute("llm.provider", _provider_kind)
            _span.set_attribute("llm.model", model)
            if max_output_tokens is not None:
                _span.set_attribute("llm.request.max_tokens", max_output_tokens)
            if self._trace_llm_io:
                _span.set_attribute(
                    "llm.request.messages",
                    json.dumps(_serialize_messages(messages)),
                )
            try:
                async with await self._rate_limiter.acquire(
                    self._rate_limit_key, max_concurrency=self._max_concurrency,
                ):
                    client = self._get_client()
                    try:
                        sdk_stream = await client.responses.create(**request)
                    except Exception as exc:
                        err = classify_openai_exception(exc)
                        logger.error(
                            "OpenResponses request failed before stream opened",
                            extra={
                                "provider_id": self._provider.id,
                                "model": model,
                                "exception": type(exc).__name__,
                            },
                        )
                        raise err from exc

                    tokens_in = 0
                    tokens_out = 0
                    state = _StreamState()
                    try:
                        async for raw in sdk_stream:
                            for event in _translate_event(raw, state):
                                if isinstance(event, Usage):
                                    tokens_in = event.input_tokens
                                    tokens_out = event.output_tokens
                                yield event
                    except Exception as exc:
                        err = classify_openai_exception(exc)
                        logger.error(
                            "OpenResponses stream aborted",
                            extra={
                                "provider_id": self._provider.id,
                                "model": model,
                                "exception": type(exc).__name__,
                            },
                        )
                        yield ChatError(
                            fatal=True,
                            code=err.code,
                            message=err.message,
                        )
                        return
                    _span.set_attribute("llm.usage.tokens_in", tokens_in)
                    _span.set_attribute("llm.usage.tokens_out", tokens_out)
                    _metrics.llm_tokens_total.labels(_provider_kind, "in").inc(tokens_in)
                    _metrics.llm_tokens_total.labels(_provider_kind, "out").inc(tokens_out)
            except Exception as _exc:
                _span.record_exception(_exc)
                _metrics.llm_failure_total.labels(_provider_kind, type(_exc).__name__).inc()
                raise
            finally:
                _metrics.llm_duration_seconds.labels(_provider_kind).observe(time.monotonic() - _t0)
