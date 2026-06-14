"""Anthropic LLM adapter — wraps anthropic.AsyncAnthropic.

Subclasses :class:`primer.int.LLM` and translates the universal chat
interface (:mod:`primer.model.chat`) onto the Anthropic Messages API
streaming surface.

See spec at ``docs/superpowers/specs/2026-04-26-anthropic-llm-design.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel as PydanticBaseModel

from primer.common.anthropic_errors import classify_anthropic_exception
from primer.int.llm import LLM
from primer.llm._timeout import _iter_with_timeout
from primer.llm._tokenizer.anthropic import count_tokens_anthropic
from primer.model.chat import (
    AudioPart,
    Citation,
    DocumentPart,
    Done,
    ExtendedEvent,
    ExtendedPart,
    ImagePart,
    Message,
    Part,
    ReasoningDelta,
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
from primer.model.chat import Error as ChatError
from primer.model.except_ import (
    ConfigError,
    ModelNotFoundError,
    UnsupportedContentError,
)
from primer.int.coordinator import RateLimiter
from primer.model.provider import (
    AnthropicConfig,
    LLMProvider,
    LLMProviderType,
)
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics


logger = logging.getLogger(__name__)


def _part_to_anthropic_block(part: Part) -> dict[str, Any]:
    """Translate one universal Part into one Anthropic content block dict."""
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}

    if isinstance(part, ImagePart):
        if part.data is not None:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.mime_type or "image/png",
                    "data": base64.b64encode(part.data).decode(),
                },
            }
        if part.url is not None:
            return {"type": "image", "source": {"type": "url", "url": part.url}}
        raise UnsupportedContentError(
            "Anthropic does not accept file_id images; provide data or url"
        )

    if isinstance(part, DocumentPart):
        if part.data is not None:
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": part.mime_type or "application/pdf",
                    "data": base64.b64encode(part.data).decode(),
                },
            }
        if part.url is not None:
            return {"type": "document", "source": {"type": "url", "url": part.url}}
        raise UnsupportedContentError(
            "Anthropic does not accept file_id documents; provide data or url"
        )

    if isinstance(part, ToolCallPart):
        return {
            "type": "tool_use",
            "id": part.id,
            "name": part.name,
            "input": part.arguments,
        }

    if isinstance(part, ToolResultPart):
        raise UnsupportedContentError(  # pragma: no cover
            "ToolResultPart should be flattened by the message walker"
        )

    if isinstance(part, ExtendedPart):
        ext = part.extended
        if isinstance(ext, AudioPart):
            raise UnsupportedContentError("Anthropic does not accept audio")
        if isinstance(ext, VideoPart):
            raise UnsupportedContentError("Anthropic does not accept video")
        raise UnsupportedContentError(  # pragma: no cover
            f"Anthropic does not support extended part type {ext.type!r}"
        )

    raise UnsupportedContentError(  # pragma: no cover
        f"unexpected part type {type(part).__name__}"
    )


def _messages_to_anthropic(
    messages: list[Message],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Walk messages -> (system_string_or_None, list_of_anthropic_messages)."""
    system_parts: list[str] = []
    out_messages: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            for part in msg.parts:
                if not isinstance(part, TextPart):
                    raise UnsupportedContentError(
                        f"system messages must contain only TextPart; "
                        f"got {type(part).__name__}"
                    )
                system_parts.append(part.text)
            continue

        if msg.role == "tool":
            blocks: list[dict[str, Any]] = []
            for part in msg.parts:
                if not isinstance(part, ToolResultPart):
                    raise UnsupportedContentError(
                        f"tool-role messages must contain only ToolResultPart; "
                        f"got {type(part).__name__}"
                    )
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": part.id,
                    "content": part.output,
                    "is_error": part.error,
                })
            out_messages.append({"role": "user", "content": blocks})
            continue

        # user / assistant
        blocks = [_part_to_anthropic_block(p) for p in msg.parts]
        out_messages.append({"role": msg.role, "content": blocks})

    system = "\n\n".join(system_parts) if system_parts else None
    return system, out_messages


_DEFAULT_MAX_TOKENS = 4096


def _tools_to_anthropic(tools: list[Tool] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "name": t.id,
            "description": t.description,
            "input_schema": t.args_schema,
        }
        for t in tools
    ]


def _tool_choice_to_anthropic(choice: ToolChoice | None) -> dict[str, Any] | None:
    if choice is None:
        return None
    if choice == "auto":
        return {"type": "auto"}
    if choice == "required":
        return {"type": "any"}
    if choice == "none":
        return {"type": "none"}
    return {"type": "tool", "name": choice}


def _response_format_to_emulation(
    fmt: type[PydanticBaseModel] | dict[str, Any] | None,
    *,
    has_tools: bool,
    has_tool_choice: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    if fmt is None:
        return None
    if has_tools:
        raise ConfigError(
            "response_format cannot be combined with tools on Anthropic; "
            "include the schema as one of your tools and use "
            "tool_choice='required' or tool_choice='structured_output'"
        )
    if has_tool_choice:
        raise ConfigError(
            "response_format cannot be combined with explicit tool_choice on Anthropic"
        )
    if isinstance(fmt, dict):
        schema = fmt
    elif isinstance(fmt, type) and issubclass(fmt, PydanticBaseModel):
        schema = fmt.model_json_schema()
    else:
        raise ConfigError(
            f"response_format must be a Pydantic class or dict; "
            f"got {type(fmt).__name__}"
        )
    synthetic_tools = [{
        "name": "structured_output",
        "description": "Emit the response in the structured shape defined by input_schema.",
        "input_schema": schema,
    }]
    synthetic_tool_choice = {"type": "tool", "name": "structured_output"}
    return synthetic_tools, synthetic_tool_choice


def _build_sampling_kwargs(
    *,
    temperature: float | None,
    top_p: float | None,
    max_output_tokens: int | None,
    stop: list[str] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if temperature is not None:
        out["temperature"] = temperature
    if top_p is not None:
        out["top_p"] = top_p
    if max_output_tokens is None:
        out["max_tokens"] = _DEFAULT_MAX_TOKENS
        logger.info(
            "max_output_tokens not provided; defaulting to %d "
            "(Anthropic API requires max_tokens). "
            "Pass max_output_tokens explicitly to override.",
            _DEFAULT_MAX_TOKENS,
        )
    else:
        out["max_tokens"] = max_output_tokens
    if stop is not None:
        out["stop_sequences"] = stop
    return out


_RECOGNISED_EXTENDED_PASSTHROUGH: frozenset[str] = frozenset({
    "top_k",
    "metadata",
    "service_tier",
    "cache_control",
    "thinking",
})


def _extract_extended_kwargs(extended: dict[str, Any] | None) -> dict[str, Any]:
    if not extended:
        return {}
    out: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in extended.items():
        if key in _RECOGNISED_EXTENDED_PASSTHROUGH:
            out[key] = value
        else:
            dropped.append(key)
    if dropped:
        logger.debug(
            "Anthropic adapter dropped unknown extended kwargs: %s",
            ", ".join(sorted(dropped)),
        )
    return out


@dataclass
class _StreamState:
    """Per-stream mutable state used by :func:`_translate_event`.

    A new instance is created at the top of every ``stream()`` call.
    """

    request_id: str | None = None
    sdk_model: str | None = None
    saw_tool_use: bool = False
    block_kinds: dict[int, str] = field(default_factory=dict)
    tool_call_meta: dict[int, dict[str, str]] = field(default_factory=dict)
    accumulated_args: dict[int, str] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    final_stop_reason: str | None = None
    emitted_stream_start: bool = False


_STOP_REASON_MAP: dict[str, StopReason] = {
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "tool_use": "tool_use",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def _map_stop_reason(reason: str | None, state: _StreamState) -> StopReason:
    """Map Anthropic ``stop_reason`` → universal :data:`StopReason`.

    ``end_turn`` collapses to ``tool_use`` when the stream emitted any
    ``tool_use`` block (the model finished mid-tool-call), else ``stop``.
    Unknown / missing reasons collapse to ``other``.
    """
    if reason is None:
        return "other"
    if reason == "end_turn":
        return "tool_use" if state.saw_tool_use else "stop"
    return _STOP_REASON_MAP.get(reason, "other")


def _citation_to_universal(citation: Any, index: int) -> Citation:
    """Translate one Anthropic citation payload to a :class:`Citation`.

    Anthropic exposes several citation shapes (``char_location``,
    ``page_location``, ``content_block_location``, web/search-result
    locations). We populate whichever fields are present.
    """
    return Citation(
        source_url=getattr(citation, "url", None),
        source_title=getattr(citation, "title", None)
        or getattr(citation, "document_title", None),
        source_id=getattr(citation, "file_id", None)
        or getattr(citation, "encrypted_index", None),
        quoted_text=getattr(citation, "cited_text", None),
        start_index=getattr(citation, "start_char_index", None),
        end_index=getattr(citation, "end_char_index", None),
        index=index,
    )


def _translate_event(  # noqa: C901  (intentional dispatch table)
    event: Any, state: _StreamState, *, model_name: str
) -> list[StreamEvent]:
    """Translate one Anthropic streaming event into universal events.

    Pure function. Returns zero or more :data:`StreamEvent`s the adapter
    should yield. Mutates ``state`` to track block kinds, tool-call
    metadata, accumulated argument fragments, and final usage / stop
    reason.
    """
    etype = getattr(event, "type", "")

    if etype == "message_start":
        msg = getattr(event, "message", None)
        usage_obj = getattr(msg, "usage", None) if msg is not None else None
        if usage_obj is not None:
            in_tok = getattr(usage_obj, "input_tokens", None)
            if in_tok is not None:
                state.input_tokens = in_tok
        request_id = getattr(msg, "id", None) if msg is not None else None
        sdk_model = getattr(msg, "model", None) if msg is not None else None
        state.request_id = request_id
        state.sdk_model = sdk_model or model_name
        state.emitted_stream_start = True
        return [
            StreamStart(
                request_id=request_id,
                model=sdk_model or model_name,
            )
        ]

    if etype == "content_block_start":
        idx = getattr(event, "index", 0)
        block = getattr(event, "content_block", None)
        block_type = getattr(block, "type", "") if block is not None else ""

        if block_type == "text":
            state.block_kinds[idx] = "text"
            return []

        if block_type == "tool_use":
            state.block_kinds[idx] = "tool_use"
            state.saw_tool_use = True
            block_id = getattr(block, "id", "") or ""
            block_name = getattr(block, "name", "") or ""
            state.tool_call_meta[idx] = {"id": block_id, "name": block_name}
            return [ToolCallStart(id=block_id, name=block_name, index=idx)]

        if block_type == "thinking":
            state.block_kinds[idx] = "thinking"
            return []

        if block_type == "server_tool_use":
            state.block_kinds[idx] = "server_tool_use"
            block_id = getattr(block, "id", "") or ""
            block_name = getattr(block, "name", "") or ""
            return [
                ExtendedEvent(
                    extended=ServerToolCallStart(
                        id=block_id, tool_name=block_name, index=idx
                    )
                )
            ]

        # Unknown block type — register kind and continue silently.
        state.block_kinds[idx] = block_type or "unknown"
        return []

    if etype == "content_block_delta":
        idx = getattr(event, "index", 0)
        delta = getattr(event, "delta", None)
        delta_type = getattr(delta, "type", "") if delta is not None else ""

        if delta_type == "text_delta":
            return [
                TextDelta(text=getattr(delta, "text", "") or "", index=idx)
            ]

        if delta_type == "input_json_delta":
            partial = getattr(delta, "partial_json", "") or ""
            state.accumulated_args[idx] = (
                state.accumulated_args.get(idx, "") + partial
            )
            meta = state.tool_call_meta.get(idx, {})
            return [
                ToolCallDelta(
                    id=meta.get("id", ""),
                    arguments_delta=partial,
                    index=idx,
                )
            ]

        if delta_type == "thinking_delta":
            return [
                ReasoningDelta(
                    text=getattr(delta, "thinking", "") or "", index=idx
                )
            ]

        if delta_type == "signature_delta":
            return [
                ReasoningDelta(
                    text="",
                    signature=getattr(delta, "signature", None),
                    index=idx,
                )
            ]

        if delta_type == "citations_delta":
            citation = getattr(delta, "citation", None)
            if citation is None:
                return []
            return [
                ExtendedEvent(extended=_citation_to_universal(citation, idx))
            ]

        return []

    if etype == "content_block_stop":
        idx = getattr(event, "index", 0)
        if state.block_kinds.get(idx) != "tool_use":
            return []
        meta = state.tool_call_meta.get(idx, {})
        raw_args = state.accumulated_args.get(idx, "") or "{}"
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return [
            ToolCallEnd(id=meta.get("id", ""), arguments=parsed, index=idx)
        ]

    if etype == "message_delta":
        delta = getattr(event, "delta", None)
        if delta is not None:
            stop_reason = getattr(delta, "stop_reason", None)
            if stop_reason is not None:
                state.final_stop_reason = stop_reason
        usage_obj = getattr(event, "usage", None)
        if usage_obj is not None:
            out_tok = getattr(usage_obj, "output_tokens", None)
            if out_tok is not None:
                state.output_tokens = out_tok
        return []

    if etype == "message_stop":
        out: list[StreamEvent] = []
        if state.input_tokens is not None and state.output_tokens is not None:
            out.append(
                Usage(
                    input_tokens=state.input_tokens,
                    output_tokens=state.output_tokens,
                    cumulative=False,
                )
            )
        raw = state.final_stop_reason or ""
        out.append(
            Done(
                stop_reason=_map_stop_reason(state.final_stop_reason, state),
                raw_reason=raw,
            )
        )
        return out

    return []


from primer.llm._trace import _serialize_messages  # noqa: E402


class AnthropicLLM(LLM):
    """Streaming LLM adapter for the Anthropic Messages API."""

    DEFAULT_MAX_TOKENS = _DEFAULT_MAX_TOKENS

    def __init__(
        self,
        provider: LLMProvider,
        *,
        rate_limiter: RateLimiter | None = None,
        trace_llm_io: bool = False,
    ) -> None:
        if provider.provider != LLMProviderType.ANTHROPIC:
            raise ConfigError(
                f"AnthropicLLM requires provider type ANTHROPIC; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, AnthropicConfig):
            raise ConfigError(
                "AnthropicLLM requires AnthropicConfig in provider.config"
            )
        # api_key is optional on the config so operators can register
        # endpoints fronted by an auth-injecting proxy; the real
        # Anthropic API will surface 401 at call time if the key is
        # actually required.

        self._provider = provider
        self._config: AnthropicConfig = provider.config
        self._client: AsyncAnthropic | None = None
        if rate_limiter is None:
            from primer.coordinator.in_memory import InMemoryRateLimiter
            rate_limiter = InMemoryRateLimiter()
        self._rate_limiter = rate_limiter
        self._rate_limit_key = f"llm:{provider.id}"
        self._max_concurrency = provider.limits.max_concurrency
        self._request_timeout_seconds = provider.limits.request_timeout_seconds
        self._trace_llm_io = trace_llm_io

        logger.info(
            "Anthropic adapter initialized",
            extra={
                "provider_id": provider.id,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
                "request_timeout_seconds": provider.limits.request_timeout_seconds,
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
        client = self._get_client()
        return await count_tokens_anthropic(
            client=client, model=model, messages=messages, tools=tools,
        )

    def _get_client(self) -> AsyncAnthropic:
        if self._client is None:
            key = (
                self._config.api_key.get_secret_value()
                if self._config.api_key is not None
                else ""
            )
            self._client = AsyncAnthropic(api_key=key)
        return self._client

    async def aclose(self) -> None:
        """Close the anthropic SDK client (releases the httpx pool).
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
        response_format: type[PydanticBaseModel] | dict[str, Any] | None = None,
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

        system, anthropic_messages = _messages_to_anthropic(messages)

        emulation = _response_format_to_emulation(
            response_format,
            has_tools=bool(tools),
            has_tool_choice=tool_choice is not None,
        )

        request: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
        }
        if system is not None:
            request["system"] = system

        request.update(
            _build_sampling_kwargs(
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
                stop=stop,
            )
        )

        if emulation is not None:
            synthetic_tools, synthetic_tool_choice = emulation
            request["tools"] = synthetic_tools
            request["tool_choice"] = synthetic_tool_choice
        else:
            tool_payload = _tools_to_anthropic(tools)
            if tool_payload is not None:
                request["tools"] = tool_payload
            choice_value = _tool_choice_to_anthropic(tool_choice)
            if choice_value is not None:
                request["tool_choice"] = choice_value

        request.update(_extract_extended_kwargs(extended))

        logger.info(
            "Anthropic stream starting",
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
                        sdk_stream = await client.messages.create(stream=True, **request)
                    except Exception as exc:
                        err = classify_anthropic_exception(exc)
                        logger.error(
                            "Anthropic request failed before stream opened",
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
                        async for raw in _iter_with_timeout(
                            sdk_stream, self._request_timeout_seconds
                        ):
                            for event in _translate_event(raw, state, model_name=model):
                                if isinstance(event, Usage):
                                    tokens_in = event.input_tokens
                                    tokens_out = event.output_tokens
                                yield event
                    except asyncio.TimeoutError as exc:
                        from primer.model.except_ import ProviderTimeoutError
                        timeout_val = self._request_timeout_seconds
                        logger.error(
                            "Anthropic stream timed out (no event in %.1f s)",
                            timeout_val,
                            extra={
                                "provider_id": self._provider.id,
                                "model": model,
                            },
                        )
                        raise ProviderTimeoutError(
                            f"Anthropic stream stalled: no event received within "
                            f"{timeout_val} s (provider_id={self._provider.id!r}, "
                            f"model={model!r})",
                            code="stream_timeout",
                        ) from exc
                    except Exception as exc:
                        err = classify_anthropic_exception(exc)
                        logger.error(
                            "Anthropic stream aborted",
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
