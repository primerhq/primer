"""Gemini LLM adapter — wraps the google-genai SDK.

Subclasses :class:`matrix.int.LLM` and translates the universal chat
interface (:mod:`matrix.model.chat`) onto the Gemini Generate Content
streaming API. Targets the Gemini API (Google AI Studio) — single
api_key auth. Vertex AI is out of scope for this adapter.

See the design spec at
``docs/superpowers/specs/2026-04-26-gemini-llm-design.md`` for the
per-Part input mapping, per-chunk stream translation, and stop-reason
mapping details.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import types as gtypes
from pydantic import BaseModel

from matrix.common.google_errors import classify_google_exception
from matrix.int.llm import LLM
from matrix.model.except_ import (
    ConfigError,
    ModelNotFoundError,
    UnsupportedContentError,
)
from matrix.model.chat import (
    AudioPart,
    Citation,
    DocumentPart,
    Done,
    Error as ChatError,
    ExtendedEvent,
    ExtendedPart,
    ImagePart,
    Logprobs,
    MediaDelta,
    Message,
    Part,
    ReasoningDelta,
    SafetyRatings,
    ServerToolCallDelta,
    ServerToolCallEnd,
    ServerToolCallStart,
    StopReason,
    StreamEvent,
    StreamStart,
    TextDelta,
    TextPart,
    TokenLogprob,
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
from matrix.model.provider import (
    GoogleConfig,
    LLMProvider,
    LLMProviderType,
)


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Input mapping: matrix.model.chat.Part -> google.genai.types.Part            #
# --------------------------------------------------------------------------- #


def _part_to_gemini(part: Part, name_lookup: dict[str, str]) -> gtypes.Part:
    """Translate one universal :class:`Part` into one Gemini ``Part``.

    Pure function, no I/O. ``name_lookup`` maps ToolCallPart ids to
    names so :class:`ToolResultPart` (which only carries an id) can
    populate the ``FunctionResponse.name`` field Gemini requires.

    The first matrix adapter that accepts every universal Part type
    natively — Gemini handles text, image, document, audio, and video.
    """
    if isinstance(part, TextPart):
        return gtypes.Part(text=part.text)

    if isinstance(part, ImagePart):
        if part.data is not None:
            return gtypes.Part(
                inline_data=gtypes.Blob(
                    mime_type=part.mime_type or "image/png",
                    data=part.data,
                )
            )
        if part.url is not None:
            return gtypes.Part(
                file_data=gtypes.FileData(
                    file_uri=part.url,
                    mime_type=part.mime_type,
                )
            )
        # file_id (validator guarantees one of the three is present)
        return gtypes.Part(
            file_data=gtypes.FileData(
                file_uri=part.file_id,
                mime_type=part.mime_type,
            )
        )

    if isinstance(part, DocumentPart):
        if part.data is not None:
            return gtypes.Part(
                inline_data=gtypes.Blob(
                    mime_type=part.mime_type or "application/pdf",
                    data=part.data,
                )
            )
        if part.url is not None:
            return gtypes.Part(
                file_data=gtypes.FileData(
                    file_uri=part.url,
                    mime_type=part.mime_type,
                )
            )
        return gtypes.Part(
            file_data=gtypes.FileData(
                file_uri=part.file_id,
                mime_type=part.mime_type,
            )
        )

    if isinstance(part, ToolCallPart):
        return gtypes.Part(
            function_call=gtypes.FunctionCall(
                id=part.id,
                name=part.name,
                args=part.arguments,
            )
        )

    if isinstance(part, ToolResultPart):
        if part.id not in name_lookup:
            raise UnsupportedContentError(
                f"ToolResultPart id={part.id!r} has no matching ToolCallPart"
            )
        return gtypes.Part(
            function_response=gtypes.FunctionResponse(
                id=part.id,
                name=name_lookup[part.id],
                response={"result": part.output},
            )
        )

    if isinstance(part, ExtendedPart):
        ext = part.extended
        if isinstance(ext, AudioPart):
            if ext.data is not None:
                return gtypes.Part(
                    inline_data=gtypes.Blob(
                        mime_type=ext.mime_type or "audio/mpeg",
                        data=ext.data,
                    )
                )
            uri = ext.url if ext.url is not None else ext.file_id
            return gtypes.Part(
                file_data=gtypes.FileData(
                    file_uri=uri,
                    mime_type=ext.mime_type,
                )
            )
        if isinstance(ext, VideoPart):
            if ext.data is not None:
                return gtypes.Part(
                    inline_data=gtypes.Blob(
                        mime_type=ext.mime_type or "video/mp4",
                        data=ext.data,
                    )
                )
            uri = ext.url if ext.url is not None else ext.file_id
            video_meta: gtypes.VideoMetadata | None = None
            if (
                ext.start_offset is not None
                or ext.end_offset is not None
                or ext.fps is not None
            ):
                video_meta = gtypes.VideoMetadata(
                    start_offset=ext.start_offset,
                    end_offset=ext.end_offset,
                    fps=ext.fps,
                )
            return gtypes.Part(
                file_data=gtypes.FileData(
                    file_uri=uri,
                    mime_type=ext.mime_type,
                ),
                video_metadata=video_meta,
            )
        raise UnsupportedContentError(  # pragma: no cover
            f"Gemini does not support extended part type {ext.type!r}"
        )

    raise UnsupportedContentError(  # pragma: no cover
        f"unexpected part type {type(part).__name__}"
    )


def _messages_to_gemini(
    messages: list[Message],
) -> tuple[str | None, list[gtypes.Content]]:
    """Walk a chat history and produce ``(system_instruction, contents)``.

    System messages are concatenated with ``"\\n\\n"`` and lifted to
    the top-level ``system_instruction`` parameter (Gemini accepts one
    string). Assistant messages are renamed to ``role="model"``. Tool
    messages are flattened into a synthesised ``role="user"`` Content
    carrying ``Part(function_response=...)`` per ToolResultPart.

    The walker maintains a running ``id -> name`` map populated when
    ToolCallParts are seen, so subsequent ToolResultParts can look up
    the name Gemini's FunctionResponse requires.
    """
    system_parts: list[str] = []
    contents: list[gtypes.Content] = []
    name_lookup: dict[str, str] = {}

    for msg in messages:
        if msg.role == "system":
            for part in msg.parts:
                if isinstance(part, TextPart):
                    system_parts.append(part.text)
                else:
                    raise UnsupportedContentError(
                        f"system messages must contain only TextPart; "
                        f"got {type(part).__name__}"
                    )
            continue

        if msg.role == "tool":
            tool_parts: list[gtypes.Part] = []
            for part in msg.parts:
                if not isinstance(part, ToolResultPart):
                    raise UnsupportedContentError(
                        f"tool-role messages must contain only ToolResultPart; "
                        f"got {type(part).__name__}"
                    )
                tool_parts.append(_part_to_gemini(part, name_lookup))
            contents.append(gtypes.Content(role="user", parts=tool_parts))
            continue

        # Pre-pass: register any ToolCallParts in this message so
        # later ToolResultParts (in subsequent messages) can look up names.
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                name_lookup[part.id] = part.name

        gemini_parts = [_part_to_gemini(part, name_lookup) for part in msg.parts]
        gemini_role = "model" if msg.role == "assistant" else "user"
        contents.append(gtypes.Content(role=gemini_role, parts=gemini_parts))

    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


# --------------------------------------------------------------------------- #
# Tool / tool-choice / response-format translators                            #
# --------------------------------------------------------------------------- #


def _tools_to_gemini(tools: list[Tool] | None) -> list[gtypes.Tool]:
    """Translate universal :class:`Tool`s into Gemini's wrapper.

    All universal tools fold into ONE ``Tool(function_declarations=[...])``
    rather than N separate Tool wrappers — Gemini accepts multiple
    declarations per wrapper, and one wrapper is cleaner.
    ``tool.toolset_id`` is caller-side correlation only and is NOT
    transmitted.
    """
    if not tools:
        return []
    return [
        gtypes.Tool(
            function_declarations=[
                gtypes.FunctionDeclaration(
                    name=t.id,
                    description=t.description,
                    parameters_json_schema=t.args_schema,
                )
                for t in tools
            ]
        )
    ]


def _tool_choice_to_gemini(choice: ToolChoice | None) -> gtypes.ToolConfig | None:
    """Translate the universal :data:`ToolChoice` into a Gemini ``ToolConfig``.

    Returns ``None`` to signal "do not include in the request"; the
    caller drops the key from the payload.

    Mapping:

    * ``None`` → omit (Gemini default is AUTO)
    * ``"auto"`` → ``mode=AUTO``
    * ``"required"`` → ``mode=ANY``
    * ``"none"`` → ``mode=NONE``
    * specific tool name → ``mode=ANY, allowed_function_names=[name]``
    """
    if choice is None:
        return None
    fcc_kwargs: dict[str, Any] = {}
    if choice == "auto":
        fcc_kwargs["mode"] = gtypes.FunctionCallingConfigMode.AUTO
    elif choice == "required":
        fcc_kwargs["mode"] = gtypes.FunctionCallingConfigMode.ANY
    elif choice == "none":
        fcc_kwargs["mode"] = gtypes.FunctionCallingConfigMode.NONE
    else:
        fcc_kwargs["mode"] = gtypes.FunctionCallingConfigMode.ANY
        fcc_kwargs["allowed_function_names"] = [choice]
    return gtypes.ToolConfig(
        function_calling_config=gtypes.FunctionCallingConfig(**fcc_kwargs)
    )


def _response_format_to_gemini(
    fmt: type[BaseModel] | dict[str, Any] | None,
) -> dict[str, Any]:
    """Translate ``response_format`` into Gemini ``GenerateContentConfig`` kwargs.

    Returns a dict so the caller can ``**``-spread into the config
    constructor. Empty dict means "no structured output".
    """
    if fmt is None:
        return {}
    if isinstance(fmt, dict):
        schema = fmt
    elif isinstance(fmt, type) and issubclass(fmt, BaseModel):
        schema = fmt.model_json_schema()
    else:
        raise ConfigError(
            f"response_format must be a Pydantic class or dict; "
            f"got {type(fmt).__name__}"
        )
    return {
        "response_mime_type": "application/json",
        "response_schema": schema,
    }


# --------------------------------------------------------------------------- #
# Sampling and extended-kwarg extraction                                       #
# --------------------------------------------------------------------------- #


def _build_sampling_kwargs(
    *,
    temperature: float | None,
    top_p: float | None,
    max_output_tokens: int | None,
    stop: list[str] | None,
) -> dict[str, Any]:
    """Forward universal sampling knobs to Gemini ``GenerateContentConfig`` fields.

    Gemini supports ``stop_sequences`` natively (unlike OpenAI Responses
    which has no native equivalent and silently drops it).
    """
    out: dict[str, Any] = {}
    if temperature is not None:
        out["temperature"] = temperature
    if top_p is not None:
        out["top_p"] = top_p
    if max_output_tokens is not None:
        out["max_output_tokens"] = max_output_tokens
    if stop is not None:
        out["stop_sequences"] = stop
    return out


_RECOGNISED_EXTENDED_PASSTHROUGH: frozenset[str] = frozenset({
    "top_k",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "safety_settings",
    "response_logprobs",
    "logprobs",
})


def _extract_extended_kwargs(extended: dict[str, Any] | None) -> dict[str, Any]:
    """Project the universal ``extended`` dict onto Gemini config kwargs.

    Recognised keys are forwarded; ``thinking_budget`` and
    ``include_thoughts`` fold into a single ``thinking_config`` dict;
    unknown keys are dropped with a single DEBUG log line listing them.
    """
    if not extended:
        return {}

    out: dict[str, Any] = {}
    thinking_kwargs: dict[str, Any] = {}
    dropped: list[str] = []

    for key, value in extended.items():
        if key == "thinking_budget":
            thinking_kwargs["thinking_budget"] = value
        elif key == "include_thoughts":
            thinking_kwargs["include_thoughts"] = value
        elif key in _RECOGNISED_EXTENDED_PASSTHROUGH:
            out[key] = value
        else:
            dropped.append(key)

    if thinking_kwargs:
        out["thinking_config"] = gtypes.ThinkingConfig(**thinking_kwargs)
    if dropped:
        logger.debug(
            "Gemini adapter dropped unknown extended kwargs: %s",
            ", ".join(sorted(dropped)),
        )
    return out


# --------------------------------------------------------------------------- #
# Stream event translation                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class _StreamState:
    """Per-stream mutable state used by :func:`_translate_chunk`."""

    next_index: int = 0
    saw_function_call: bool = False
    last_usage_metadata: Any = None
    emitted_stream_start: bool = False
    last_text_index: int = 0
    server_tool_index: dict[str, int] = field(default_factory=dict)


def _next_index(state: _StreamState) -> int:
    """Allocate and return the next flat block index."""
    idx = state.next_index
    state.next_index += 1
    return idx


def _map_finish_reason(reason: str, state: _StreamState) -> StopReason:
    """Translate a Gemini ``FinishReason`` (string) to a universal ``StopReason``."""
    if reason == "STOP":
        return "tool_use" if state.saw_function_call else "stop"
    if reason == "MAX_TOKENS":
        return "max_tokens"
    if reason == "STOP_SEQUENCE":
        return "stop_sequence"
    if reason in {
        "SAFETY",
        "RECITATION",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "BLOCKLIST",
    }:
        return "content_filter"
    if reason == "MALFORMED_FUNCTION_CALL":
        return "error"
    return "other"


def _build_usage(usage_obj: Any) -> Usage | None:
    """Translate a Gemini ``usage_metadata`` object to a universal :class:`Usage`."""
    if usage_obj is None:
        return None
    input_tokens = getattr(usage_obj, "prompt_token_count", None)
    output_tokens = getattr(usage_obj, "candidates_token_count", None)
    if input_tokens is None or output_tokens is None:
        return None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=getattr(usage_obj, "cached_content_token_count", None),
        reasoning_tokens=getattr(usage_obj, "thoughts_token_count", None),
        cumulative=True,
    )


def _part_to_events(
    part: Any, state: _StreamState
) -> list[StreamEvent]:
    """Translate one Gemini response ``Part`` (from a chunk) into stream events."""
    out: list[StreamEvent] = []

    text = getattr(part, "text", None)
    if text:
        thought = getattr(part, "thought", None)
        idx = _next_index(state)
        state.last_text_index = idx
        if thought:
            out.append(ReasoningDelta(text=text, index=idx))
        else:
            out.append(TextDelta(text=text, index=idx))
        return out

    fc = getattr(part, "function_call", None)
    if fc is not None:
        state.saw_function_call = True
        idx = _next_index(state)
        call_id = getattr(fc, "id", None) or f"call_{idx}"
        name = getattr(fc, "name", "") or ""
        args = getattr(fc, "args", None) or {}
        out.append(ToolCallStart(id=call_id, name=name, index=idx))
        out.append(
            ToolCallDelta(id=call_id, arguments_delta=json.dumps(args), index=idx)
        )
        out.append(ToolCallEnd(id=call_id, arguments=args, index=idx))
        return out

    inline = getattr(part, "inline_data", None)
    if inline is not None:
        mime = getattr(inline, "mime_type", "") or ""
        data = getattr(inline, "data", b"")
        idx = _next_index(state)
        if mime.startswith("audio/"):
            out.append(MediaDelta(kind="audio", data=data, mime_type=mime, index=idx))
        elif mime.startswith("image/"):
            out.append(MediaDelta(kind="image", data=data, mime_type=mime, index=idx))
        return out

    ec = getattr(part, "executable_code", None)
    if ec is not None:
        idx = _next_index(state)
        synth_id = f"code_exec_{idx}"
        state.server_tool_index[synth_id] = idx
        out.append(
            ExtendedEvent(
                extended=ServerToolCallStart(
                    id=synth_id, tool_name="code_execution", index=idx
                )
            )
        )
        code_text = getattr(ec, "code", "") or ""
        out.append(
            ExtendedEvent(
                extended=ServerToolCallDelta(id=synth_id, text=code_text, index=idx)
            )
        )
        return out

    cer = getattr(part, "code_execution_result", None)
    if cer is not None:
        # Match to the most recent code_exec id.
        if state.server_tool_index:
            synth_id, idx = next(reversed(state.server_tool_index.items()))
        else:  # pragma: no cover  (defensive — shouldn't happen if SDK emits ec first)
            idx = _next_index(state)
            synth_id = f"code_exec_{idx}"
        out.append(
            ExtendedEvent(
                extended=ServerToolCallEnd(
                    id=synth_id,
                    result={
                        "outcome": getattr(cer, "outcome", None),
                        "output": getattr(cer, "output", None),
                    },
                    index=idx,
                )
            )
        )
        return out

    # Unknown part type — DEBUG and skip.
    logger.debug("Gemini adapter ignoring unknown Part: %r", part)
    return out


def _grounding_to_citations(
    grounding_metadata: Any, state: _StreamState
) -> list[StreamEvent]:
    """Extract grounding chunks as Citation extended events."""
    if grounding_metadata is None:
        return []
    chunks = getattr(grounding_metadata, "grounding_chunks", None) or []
    out: list[StreamEvent] = []
    for ch in chunks:
        web = getattr(ch, "web", None)
        rc = getattr(ch, "retrieved_context", None)
        out.append(
            ExtendedEvent(
                extended=Citation(
                    source_url=getattr(web, "uri", None) if web else None,
                    source_title=getattr(web, "title", None) if web else None,
                    source_id=getattr(rc, "uri", None) if rc else None,
                    quoted_text=None,
                    start_index=None,
                    end_index=None,
                    index=state.last_text_index,
                )
            )
        )
    return out


def _safety_ratings_to_event(
    safety_ratings: Any,
) -> list[StreamEvent]:
    """Translate a list of SafetyRating objects to a SafetyRatings extended event."""
    if not safety_ratings:
        return []
    ratings = {
        getattr(r, "category", "UNKNOWN"): getattr(r, "probability", "UNKNOWN")
        for r in safety_ratings
    }
    return [ExtendedEvent(extended=SafetyRatings(ratings=ratings, index=None))]


def _logprobs_to_event(
    logprobs_result: Any, state: _StreamState
) -> list[StreamEvent]:
    """Translate logprobs_result into a Logprobs extended event."""
    if logprobs_result is None:
        return []
    chosen = getattr(logprobs_result, "chosen_candidates", None) or []
    if not chosen:
        return []
    tokens = [
        TokenLogprob(
            token=getattr(c, "token", "") or "",
            logprob=getattr(c, "log_probability", 0.0),
            top_alternatives=None,
        )
        for c in chosen
    ]
    return [ExtendedEvent(extended=Logprobs(tokens=tokens, index=state.last_text_index))]


def _translate_chunk(
    chunk: Any, state: _StreamState, model_name: str
) -> list[StreamEvent]:
    """Translate one Gemini ``GenerateContentResponse`` chunk into stream events.

    Pure function (mutates ``state``). Emits StreamStart on first chunk,
    walks parts, walks per-chunk metadata (grounding / safety / logprobs),
    and on the final chunk (finish_reason set) emits cumulative Usage +
    Done.
    """
    out: list[StreamEvent] = []

    if not state.emitted_stream_start:
        state.emitted_stream_start = True
        out.append(
            StreamStart(
                request_id=getattr(chunk, "response_id", None),
                model=model_name,
            )
        )

    candidates = getattr(chunk, "candidates", None) or []
    candidate = candidates[0] if candidates else None

    if candidate is not None:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or [] if content else []
        for part in parts:
            out.extend(_part_to_events(part, state))

        out.extend(_grounding_to_citations(
            getattr(candidate, "grounding_metadata", None), state
        ))
        out.extend(_safety_ratings_to_event(
            getattr(candidate, "safety_ratings", None)
        ))
        out.extend(_logprobs_to_event(
            getattr(candidate, "logprobs_result", None), state
        ))

    usage_metadata = getattr(chunk, "usage_metadata", None)
    if usage_metadata is not None:
        state.last_usage_metadata = usage_metadata

    finish_reason = getattr(candidate, "finish_reason", None) if candidate else None
    if finish_reason is not None:
        # Final chunk: emit cumulative Usage + Done.
        usage_event = _build_usage(state.last_usage_metadata)
        if usage_event is not None:
            out.append(usage_event)
        # finish_reason may be an enum or string; coerce to its name.
        reason_str = getattr(finish_reason, "name", None) or str(finish_reason)
        out.append(
            Done(
                stop_reason=_map_finish_reason(reason_str, state),
                raw_reason=reason_str,
            )
        )

    return out


class GeminiLLM(LLM):
    """Streaming LLM adapter for the Gemini API."""

    def __init__(self, provider: LLMProvider) -> None:
        if provider.provider != LLMProviderType.GEMINI:
            raise ConfigError(
                f"GeminiLLM requires provider type GEMINI; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, GoogleConfig):
            raise ConfigError(
                "GeminiLLM requires GoogleConfig in provider.config"
            )
        if not provider.config.api_key.get_secret_value():
            raise ConfigError("api_key is required for GeminiLLM")

        self._provider = provider
        self._config: GoogleConfig = provider.config
        self._client: genai.Client | None = None
        self._semaphore = asyncio.Semaphore(provider.limits.max_concurrency)

        logger.info(
            "Gemini adapter initialized",
            extra={
                "provider_id": provider.id,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
            },
        )

    async def list_models(self) -> Iterable[str]:
        return [m.name for m in self._provider.models]

    def _get_client(self) -> genai.Client:
        """Construct the genai.Client lazily on first use."""
        if self._client is None:
            self._client = genai.Client(
                api_key=self._config.api_key.get_secret_value()
            )
        return self._client

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

        system_instruction, contents = _messages_to_gemini(messages)
        gemini_tools = _tools_to_gemini(tools)
        tool_config = _tool_choice_to_gemini(tool_choice)
        sampling_kwargs = _build_sampling_kwargs(
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            stop=stop,
        )
        format_kwargs = _response_format_to_gemini(response_format)
        ext_kwargs = _extract_extended_kwargs(extended)

        config_kwargs: dict[str, Any] = {}
        if system_instruction is not None:
            config_kwargs["system_instruction"] = system_instruction
        if gemini_tools:
            config_kwargs["tools"] = gemini_tools
        if tool_config is not None:
            config_kwargs["tool_config"] = tool_config
        config_kwargs.update(sampling_kwargs)
        config_kwargs.update(format_kwargs)
        config_kwargs.update(ext_kwargs)

        config = gtypes.GenerateContentConfig(**config_kwargs)

        logger.info(
            "Gemini stream starting",
            extra={
                "provider_id": self._provider.id,
                "model": model,
                "message_count": len(messages),
                "tool_count": len(tools) if tools else 0,
            },
        )

        async with self._semaphore:
            client = self._get_client()
            try:
                sdk_stream = await client.aio.models.generate_content_stream(
                    model=model,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                err = classify_google_exception(exc)
                logger.error(
                    "Gemini request failed before stream opened",
                    extra={
                        "provider_id": self._provider.id,
                        "model": model,
                        "exception": type(exc).__name__,
                    },
                )
                raise err from exc

            state = _StreamState()
            try:
                async for chunk in sdk_stream:
                    for ev in _translate_chunk(chunk, state, model_name=model):
                        yield ev
            except Exception as exc:
                err = classify_google_exception(exc)
                logger.error(
                    "Gemini stream aborted",
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
