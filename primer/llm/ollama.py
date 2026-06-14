"""Ollama LLM adapter — wraps ollama.AsyncClient.

Subclasses :class:`primer.int.LLM` and translates the universal chat
interface (:mod:`primer.model.chat`) onto the Ollama ``client.chat``
streaming surface.

Targets local or remote Ollama HTTP servers. Supports text + inline
images. Does not support tool_choice (Ollama doesn't expose one) —
caller-supplied tool_choice is silently dropped with a DEBUG log.

See spec at ``docs/superpowers/specs/2026-04-26-ollama-llm-design.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx
import ollama
from pydantic import BaseModel as PydanticBaseModel

from primer.int.llm import LLM
from primer.llm._timeout import _iter_with_timeout
from primer.llm._tokenizer.hf import count_tokens_hf
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    Done,
    ExtendedPart,
    ImagePart,
    Message,
    ReasoningDelta,
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
    AuthenticationError,
    BadRequestError,
    ConfigError,
    PrimerError,
    ModelNotFoundError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
    UnsupportedContentError,
)
from primer.int.coordinator import RateLimiter
from primer.model.provider import (
    LLMProvider,
    LLMProviderType,
    OllamaConfig,
)
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Inline classifier                                                            #
# --------------------------------------------------------------------------- #


def _classify_ollama_exception(exc: Exception) -> PrimerError:
    """Map ollama / httpx exceptions onto the primer exception hierarchy."""
    if isinstance(exc, ollama.ResponseError):
        status = getattr(exc, "status_code", None)
        msg = str(exc) or "Ollama rejected the request"
        if status in (401, 403):
            return AuthenticationError(
                "Ollama authentication failed",
                status_code=status,
                cause=exc,
            )
        if status == 429:
            return RateLimitError(
                "Ollama rate limit exceeded",
                status_code=status,
                cause=exc,
            )
        if status is not None and 400 <= status < 500:
            return BadRequestError(msg, status_code=status, cause=exc)
        if status is not None and status >= 500:
            return ServerError(
                f"Ollama server error ({status})",
                status_code=status,
                cause=exc,
            )
        return ProviderError(msg, status_code=status, cause=exc)
    if isinstance(exc, ollama.RequestError):
        return NetworkError(
            f"Ollama request failure: {type(exc).__name__}",
            cause=exc,
        )
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return NetworkError(
            f"Ollama network failure: {type(exc).__name__}",
            cause=exc,
        )
    return ProviderError(str(exc), cause=exc)


# --------------------------------------------------------------------------- #
# Input mapping                                                                #
# --------------------------------------------------------------------------- #


def _messages_to_ollama(messages: list[Message]) -> list[dict[str, Any]]:
    """Walk universal Messages -> list of Ollama message dicts."""
    out: list[dict[str, Any]] = []
    name_lookup: dict[str, str] = {}

    for msg in messages:
        if msg.role == "tool":
            for part in msg.parts:
                if not isinstance(part, ToolResultPart):
                    raise UnsupportedContentError(
                        f"tool-role messages must contain only ToolResultPart; "
                        f"got {type(part).__name__}"
                    )
                tool_name = name_lookup.get(part.id, "")
                out.append({
                    "role": "tool",
                    "content": part.output,
                    "tool_name": tool_name,
                })
            continue

        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                name_lookup[part.id] = part.name

        content_parts: list[str] = []
        images: list[bytes] = []
        tool_calls: list[dict[str, Any]] = []
        for part in msg.parts:
            if isinstance(part, TextPart):
                content_parts.append(part.text)
            elif isinstance(part, ImagePart):
                if part.data is None:
                    raise UnsupportedContentError(
                        "Ollama requires inline image data; pre-fetch URL to bytes"
                    )
                images.append(part.data)
            elif isinstance(part, DocumentPart):
                raise UnsupportedContentError("Ollama does not accept documents")
            elif isinstance(part, ToolCallPart):
                tool_calls.append({
                    "function": {
                        "name": part.name,
                        "arguments": part.arguments,
                    }
                })
            elif isinstance(part, ToolResultPart):
                raise UnsupportedContentError(  # pragma: no cover
                    "ToolResultPart should be flattened by the message walker"
                )
            elif isinstance(part, ExtendedPart):
                ext = part.extended
                ext_name = type(ext).__name__
                if "Audio" in ext_name:
                    raise UnsupportedContentError("Ollama does not accept audio")
                if "Video" in ext_name:
                    raise UnsupportedContentError("Ollama does not accept video")
                raise UnsupportedContentError(  # pragma: no cover
                    f"unsupported extended type {ext_name}"
                )

        msg_dict: dict[str, Any] = {
            "role": msg.role,
            "content": "\n".join(content_parts),
        }
        if images:
            msg_dict["images"] = images
        if tool_calls:
            msg_dict["tool_calls"] = tool_calls
        out.append(msg_dict)

    return out


# --------------------------------------------------------------------------- #
# Tools / response_format / options                                            #
# --------------------------------------------------------------------------- #


def _tools_to_ollama(tools: list[Tool] | None) -> list[dict[str, Any]] | None:
    """Translate universal Tools into Ollama's nested function shape."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.id,
                "description": t.description,
                "parameters": t.args_schema,
            },
        }
        for t in tools
    ]


def _maybe_log_unsupported_tool_choice(choice: ToolChoice | None) -> None:
    """Log DEBUG if caller passed a non-None tool_choice (Ollama doesn't accept)."""
    if choice is not None:
        logger.debug(
            "Ollama adapter: tool_choice=%r is not supported by Ollama; ignoring",
            choice,
        )


def _response_format_to_ollama(
    fmt: type[PydanticBaseModel] | dict[str, Any] | None,
) -> Any:
    """Translate response_format to Ollama's ``format`` parameter."""
    if fmt is None:
        return None
    if isinstance(fmt, dict):
        return fmt
    if isinstance(fmt, type) and issubclass(fmt, PydanticBaseModel):
        return fmt.model_json_schema()
    raise ConfigError(
        f"response_format must be a Pydantic class or dict; "
        f"got {type(fmt).__name__}"
    )


_OPTIONS_KEYS: frozenset[str] = frozenset({
    "top_k", "seed", "repeat_penalty", "frequency_penalty",
    "presence_penalty", "mirostat", "mirostat_tau", "mirostat_eta",
    "tfs_z", "typical_p", "num_ctx", "num_batch", "num_gpu",
})

_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"keep_alive", "think"})


def _build_options_and_kwargs(
    *,
    temperature: float | None,
    top_p: float | None,
    max_output_tokens: int | None,
    stop: list[str] | None,
    extended: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build (options_dict, top_level_kwargs) for Ollama's chat()."""
    options: dict[str, Any] = {}
    if temperature is not None:
        options["temperature"] = temperature
    if top_p is not None:
        options["top_p"] = top_p
    if max_output_tokens is not None:
        options["num_predict"] = max_output_tokens
    if stop is not None:
        options["stop"] = stop

    top_level: dict[str, Any] = {}
    dropped: list[str] = []
    if extended:
        for key, value in extended.items():
            if key in _OPTIONS_KEYS:
                options[key] = value
            elif key in _TOP_LEVEL_KEYS:
                top_level[key] = value
            else:
                dropped.append(key)
    if dropped:
        logger.debug(
            "Ollama adapter dropped unknown extended kwargs: %s",
            ", ".join(sorted(dropped)),
        )
    return options, top_level


# --------------------------------------------------------------------------- #
# Stream translation                                                           #
# --------------------------------------------------------------------------- #


@dataclass
class _StreamState:
    sdk_model: str | None = None
    saw_tool_call: bool = False
    next_index: int = 0
    text_index: int | None = None
    thinking_index: int | None = None
    final_done_reason: str | None = None
    prompt_tokens: int | None = None
    eval_tokens: int | None = None
    emitted_stream_start: bool = False


def _next_index(state: _StreamState) -> int:
    idx = state.next_index
    state.next_index += 1
    return idx


def _map_stop_reason(reason: str | None, state: _StreamState) -> StopReason:
    if reason == "stop":
        return "tool_use" if state.saw_tool_call else "stop"
    if reason == "length":
        return "max_tokens"
    if reason == "load":
        return "other"
    return "other"


def _translate_chunk(
    chunk: Any, state: _StreamState, model_name: str
) -> list[StreamEvent]:
    """Translate one Ollama ChatResponse chunk into stream events."""
    out: list[StreamEvent] = []

    if not state.emitted_stream_start:
        state.emitted_stream_start = True
        out.append(StreamStart(
            request_id=None,
            model=getattr(chunk, "model", None) or model_name,
        ))

    message = getattr(chunk, "message", None)
    if message is not None:
        text = getattr(message, "content", None)
        if text:
            if state.text_index is None:
                state.text_index = _next_index(state)
            out.append(TextDelta(text=text, index=state.text_index))

        thinking = getattr(message, "thinking", None)
        if thinking:
            if state.thinking_index is None:
                state.thinking_index = _next_index(state)
            out.append(ReasoningDelta(text=thinking, index=state.thinking_index))

        tool_calls = getattr(message, "tool_calls", None) or []
        for call in tool_calls:
            state.saw_tool_call = True
            idx = _next_index(state)
            synth_id = f"call_{idx}"
            fn = getattr(call, "function", None)
            name = getattr(fn, "name", "") if fn is not None else ""
            args = getattr(fn, "arguments", None) if fn is not None else None
            args = args or {}
            out.append(ToolCallStart(id=synth_id, name=name, index=idx))
            out.append(ToolCallDelta(
                id=synth_id, arguments_delta=json.dumps(args), index=idx
            ))
            out.append(ToolCallEnd(id=synth_id, arguments=args, index=idx))

    if getattr(chunk, "done", False):
        state.final_done_reason = getattr(chunk, "done_reason", None)
        prompt_count = getattr(chunk, "prompt_eval_count", None)
        eval_count = getattr(chunk, "eval_count", None)
        if prompt_count is not None:
            state.prompt_tokens = prompt_count
        if eval_count is not None:
            state.eval_tokens = eval_count
        if state.prompt_tokens is not None and state.eval_tokens is not None:
            out.append(Usage(
                input_tokens=state.prompt_tokens,
                output_tokens=state.eval_tokens,
                cumulative=False,
            ))
        out.append(Done(
            stop_reason=_map_stop_reason(state.final_done_reason, state),
            raw_reason=state.final_done_reason or "unknown",
        ))

    return out


# --------------------------------------------------------------------------- #
# Adapter                                                                      #
# --------------------------------------------------------------------------- #


from primer.llm._trace import _serialize_messages  # noqa: E402


class OllamaLLM(LLM):
    """Streaming LLM adapter for the Ollama HTTP API."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        rate_limiter: RateLimiter | None = None,
        trace_llm_io: bool = False,
    ) -> None:
        if provider.provider != LLMProviderType.OLLAMA:
            raise ConfigError(
                f"OllamaLLM requires provider type OLLAMA; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, OllamaConfig):
            raise ConfigError(
                "OllamaLLM requires OllamaConfig in provider.config"
            )

        self._provider = provider
        self._config: OllamaConfig = provider.config
        self._client: ollama.AsyncClient | None = None
        if rate_limiter is None:
            from primer.coordinator.in_memory import InMemoryRateLimiter
            rate_limiter = InMemoryRateLimiter()
        self._rate_limiter = rate_limiter
        self._rate_limit_key = f"llm:{provider.id}"
        self._max_concurrency = provider.limits.max_concurrency
        self._request_timeout_seconds = provider.limits.request_timeout_seconds
        self._trace_llm_io = trace_llm_io

        logger.info(
            "Ollama adapter initialized",
            extra={
                "provider_id": provider.id,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
                "request_timeout_seconds": provider.limits.request_timeout_seconds,
                "url": str(provider.config.url),
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
        import asyncio
        return await asyncio.to_thread(
            count_tokens_hf, model=model, messages=messages, tools=tools,
        )

    def _get_client(self) -> ollama.AsyncClient:
        if self._client is None:
            headers: dict[str, str] = {}
            if self._config.api_key is not None:
                key_value = self._config.api_key.get_secret_value()
                if key_value:
                    headers["Authorization"] = f"Bearer {key_value}"
            self._client = ollama.AsyncClient(
                host=str(self._config.url),
                headers=headers or None,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the ollama SDK client (releases the httpx pool).
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

        ollama_messages = _messages_to_ollama(messages)
        ollama_tools = _tools_to_ollama(tools)
        _maybe_log_unsupported_tool_choice(tool_choice)
        ollama_format = _response_format_to_ollama(response_format)
        options, top_level = _build_options_and_kwargs(
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            stop=stop,
            extended=extended,
        )

        request: dict[str, Any] = {
            "model": model,
            "messages": ollama_messages,
            "stream": True,
        }
        if ollama_tools is not None:
            request["tools"] = ollama_tools
        if ollama_format is not None:
            request["format"] = ollama_format
        if options:
            request["options"] = options
        request.update(top_level)

        logger.info(
            "Ollama stream starting",
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
                        sdk_stream = await client.chat(**request)
                    except Exception as exc:
                        err = _classify_ollama_exception(exc)
                        logger.error(
                            "Ollama request failed before stream opened",
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
                        async for chunk in _iter_with_timeout(
                            sdk_stream, self._request_timeout_seconds
                        ):
                            for ev in _translate_chunk(chunk, state, model_name=model):
                                if isinstance(ev, Usage):
                                    tokens_in = ev.input_tokens
                                    tokens_out = ev.output_tokens
                                yield ev
                    except asyncio.TimeoutError as exc:
                        from primer.model.except_ import ProviderTimeoutError
                        timeout_val = self._request_timeout_seconds
                        logger.error(
                            "Ollama stream timed out (no event in %.1f s)",
                            timeout_val,
                            extra={
                                "provider_id": self._provider.id,
                                "model": model,
                            },
                        )
                        raise ProviderTimeoutError(
                            f"Ollama stream stalled: no event received within "
                            f"{timeout_val} s (provider_id={self._provider.id!r}, "
                            f"model={model!r})",
                            code="stream_timeout",
                        ) from exc
                    except Exception as exc:
                        err = _classify_ollama_exception(exc)
                        logger.error(
                            "Ollama stream aborted",
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
