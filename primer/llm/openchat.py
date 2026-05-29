"""OpenChat LLM adapter — wraps the OpenAI Chat Completions API.

Subclasses :class:`primer.int.LLM` and translates the universal chat
interface (:mod:`primer.model.chat`) onto the legacy OpenAI
``/v1/chat/completions`` wire format. Targets real OpenAI, LM Studio,
Ollama's OpenAI shim, vLLM, and any other compatible server via the
:class:`OpenChatFlavor` discriminator on the provider config.

Parallel structure to :mod:`primer.llm.openresponses`. Shared helpers
live in :mod:`primer.llm._openai_common`.
"""

from __future__ import annotations

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
from primer.llm._openai_common import (
    build_sampling_params as _build_sampling_params_impl,
)
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    Done,
    Error as ChatError,
    ExtendedPart,
    ImagePart,
    Message,
    Part,
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
from primer.model.except_ import ConfigError, ModelNotFoundError, UnsupportedContentError
from primer.model.provider import (
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FlavorPolicy:
    """Per-flavor behavioural knobs for the OpenChat adapter.

    Attributes
    ----------
    require_api_key
        When True, an absent or empty ``api_key`` raises
        :class:`ConfigError` at construction time.
    """

    require_api_key: bool


_POLICY_BY_FLAVOR: dict[OpenChatFlavor, _FlavorPolicy] = {
    OpenChatFlavor.OPENAI: _FlavorPolicy(require_api_key=True),
    OpenChatFlavor.LMSTUDIO: _FlavorPolicy(require_api_key=False),
    OpenChatFlavor.OLLAMA: _FlavorPolicy(require_api_key=False),
    OpenChatFlavor.VLLM: _FlavorPolicy(require_api_key=False),
    OpenChatFlavor.OTHER: _FlavorPolicy(require_api_key=True),
}


def _part_to_content(part: Part) -> dict[str, Any]:
    """Translate one universal :class:`Part` into a Chat Completions content dict.

    Pure function, no I/O. Raises :class:`UnsupportedContentError` for
    parts the Chat Completions API does not accept.
    """
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}

    if isinstance(part, ImagePart):
        if part.file_id is not None:
            raise UnsupportedContentError(
                "Chat Completions does not accept image input by file_id; "
                "fetch the bytes and pass an ImagePart(data=...) instead"
            )
        if part.data is not None:
            mime = part.mime_type or "application/octet-stream"
            url = f"data:{mime};base64,{base64.b64encode(part.data).decode()}"
        else:
            url = part.url  # type: ignore[assignment]
        image_url: dict[str, Any] = {"url": url}
        if part.detail is not None:
            image_url["detail"] = part.detail
        return {"type": "image_url", "image_url": image_url}

    if isinstance(part, DocumentPart):
        raise UnsupportedContentError(
            "Chat Completions does not accept document input; "
            "extract text from the document and pass a TextPart instead"
        )

    if isinstance(part, ExtendedPart):
        ext = part.extended
        if isinstance(ext, AudioPart):
            raise UnsupportedContentError(
                "Chat Completions does not accept audio input on this adapter"
            )
        if isinstance(ext, VideoPart):
            raise UnsupportedContentError(
                "Chat Completions does not accept video input"
            )
        raise UnsupportedContentError(
            f"Chat Completions does not support extended part type {ext.type!r}"
        )

    raise UnsupportedContentError(  # pragma: no cover
        f"unexpected part type {type(part).__name__}"
    )


def _messages_to_chat(messages: list[Message]) -> list[dict[str, Any]]:
    """Walk a chat history and produce Chat Completions ``messages`` rows.

    Mapping rules:

    * ``role="system"`` -> one row with string ``content`` joining all
      :class:`TextPart` text values.
    * ``role="user"`` -> if the message is text-only, ``content`` is a
      plain string; if any image part is present, ``content`` is the
      multimodal content array.
    * ``role="assistant"`` -> text concatenated into ``content`` (or
      ``None`` when there is no text), with any :class:`ToolCallPart`
      flattened into the ``tool_calls`` array.
    * ``role="tool"`` -> one row per :class:`ToolResultPart`, each with
      ``tool_call_id`` echoing the assistant's call id.
    """
    rows: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "tool":
            for part in msg.parts:
                if not isinstance(part, ToolResultPart):
                    raise UnsupportedContentError(
                        f"tool-role messages must contain only ToolResultPart; "
                        f"got {type(part).__name__}"
                    )
                rows.append(
                    {
                        "role": "tool",
                        "tool_call_id": part.id,
                        "content": part.output,
                    }
                )
            continue

        text_chunks: list[str] = []
        non_text_contents: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []

        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                tool_calls.append(
                    {
                        "id": part.id,
                        "type": "function",
                        "function": {
                            "name": part.name,
                            "arguments": json.dumps(part.arguments),
                        },
                    }
                )
            elif isinstance(part, ToolResultPart):
                raise UnsupportedContentError(
                    "ToolResultPart is only valid inside a tool-role message"
                )
            elif isinstance(part, TextPart):
                text_chunks.append(part.text)
            else:
                non_text_contents.append(_part_to_content(part))

        if non_text_contents:
            content: Any = [
                {"type": "text", "text": "".join(text_chunks)}
            ] if text_chunks else []
            content.extend(non_text_contents)
        elif text_chunks:
            content = "".join(text_chunks)
        else:
            content = None

        row: dict[str, Any] = {"role": msg.role, "content": content}
        if tool_calls:
            row["tool_calls"] = tool_calls
        rows.append(row)

    return rows


def _tool_to_chat(tool: Tool) -> dict[str, Any]:
    """Translate a universal :class:`Tool` into one Chat Completions tool dict.

    The Chat Completions envelope nests the function-spec fields under
    ``function:`` — unlike the Responses envelope which inlines them.
    ``tool.toolset_id`` is caller-side correlation only and is not
    transmitted.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.id,
            "description": tool.description,
            "parameters": tool.args_schema,
        },
    }


def _tool_choice_to_chat(choice: ToolChoice | None) -> Any:
    """Translate the universal :data:`ToolChoice` to the Chat Completions value.

    Returns ``None`` to signal "do not include in the request"; the
    caller must drop the key from the payload.
    """
    if choice is None:
        return None
    if choice in ("auto", "required", "none"):
        return choice
    return {"type": "function", "function": {"name": choice}}


def _build_sampling_params(
    *,
    temperature: float | None,
    top_p: float | None,
    max_output_tokens: int | None,
    stop: list[str] | None,
) -> dict[str, Any]:
    """Forward sampling knobs to the Chat Completions wire format.

    Delegates to the shared builder with ``target="chat_completions"`` —
    ``max_tokens`` is the cap key, ``stop`` is passed through natively.
    """
    return _build_sampling_params_impl(
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        stop=stop,
        target="chat_completions",
    )


_RECOGNISED_EXTENDED_PASSTHROUGH: frozenset[str] = frozenset({
    "parallel_tool_calls",
    "presence_penalty",
    "frequency_penalty",
    "logprobs",
    "top_logprobs",
    "seed",
    "user",
})


def _extract_extended_kwargs(extended: dict[str, Any] | None) -> dict[str, Any]:
    """Project the universal ``extended`` dict onto Chat Completions kwargs.

    Recognised keys are forwarded; unknown keys are dropped with a
    single DEBUG log line listing them. Chat Completions has no
    reasoning channel, so ``reasoning_effort`` and
    ``reasoning_summary`` are treated as unknown and dropped.
    """
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
            "OpenChat adapter dropped unknown extended kwargs: %s",
            ", ".join(sorted(dropped)),
        )
    return out


def _response_format_to_param(
    fmt: type[BaseModel] | dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Translate ``response_format`` into the Chat Completions parameter.

    Returns ``None`` to signal "do not include"; the caller drops the
    key. Emits the root-level ``json_schema`` shape, not the Responses
    ``text.format`` nesting.
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
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": schema,
            "strict": True,
        },
    }


@dataclass
class _ToolCallInProgress:
    """Tracked state for one tool call as it streams in."""

    call_id: str
    name: str
    arguments_buffer: str = ""
    index: int = 0


@dataclass
class _StreamState:
    """Per-stream mutable state used by :func:`_translate_chunk`."""

    stream_started: bool = False
    saw_function_call: bool = False
    tool_calls: dict[int, _ToolCallInProgress] = field(default_factory=dict)
    request_id: str | None = None
    model: str = ""


def _build_usage(usage_obj: Any) -> Usage | None:
    """Translate a Chat Completions ``usage`` object to a :class:`Usage` event."""
    if usage_obj is None:
        return None
    prompt = getattr(usage_obj, "prompt_tokens", None)
    completion = getattr(usage_obj, "completion_tokens", None)
    if prompt is None or completion is None:
        return None
    return Usage(
        input_tokens=prompt,
        output_tokens=completion,
        cached_input_tokens=None,
        reasoning_tokens=None,
        cumulative=False,
    )


def _map_finish_reason(reason: str | None) -> StopReason:
    if reason == "stop":
        return "stop"
    if reason == "length":
        return "max_tokens"
    if reason == "tool_calls":
        return "tool_use"
    if reason == "content_filter":
        return "content_filter"
    return "other"


def _translate_chunk(  # noqa: C901
    chunk: Any, state: _StreamState
) -> list[StreamEvent]:
    """Translate one Chat Completions streaming chunk into universal events.

    Pure function. Mutates ``state`` to track per-tool-call buffers and
    whether the stream has emitted its initial :class:`StreamStart`.
    """
    out: list[StreamEvent] = []
    choices = getattr(chunk, "choices", None) or []

    for choice in choices:
        delta = getattr(choice, "delta", None)
        if delta is not None:
            role = getattr(delta, "role", None)
            if role and not state.stream_started:
                state.stream_started = True
                state.request_id = getattr(chunk, "id", None)
                state.model = getattr(chunk, "model", "") or ""
                out.append(
                    StreamStart(
                        request_id=state.request_id, model=state.model,
                    )
                )

            content = getattr(delta, "content", None)
            if content:
                out.append(TextDelta(text=content, index=0))

            tool_calls = getattr(delta, "tool_calls", None) or []
            for tc in tool_calls:
                tc_index = getattr(tc, "index", 0)
                tc_id = getattr(tc, "id", None)
                fn = getattr(tc, "function", None)
                fn_name = getattr(fn, "name", None) if fn is not None else None
                fn_args = getattr(fn, "arguments", None) if fn is not None else None

                existing = state.tool_calls.get(tc_index)
                if existing is None and tc_id and fn_name:
                    in_progress = _ToolCallInProgress(
                        call_id=tc_id, name=fn_name, index=tc_index,
                    )
                    if fn_args:
                        in_progress.arguments_buffer = fn_args
                    state.tool_calls[tc_index] = in_progress
                    state.saw_function_call = True
                    out.append(
                        ToolCallStart(
                            id=in_progress.call_id,
                            name=in_progress.name,
                            index=tc_index,
                        )
                    )
                    if fn_args:
                        out.append(
                            ToolCallDelta(
                                id=in_progress.call_id,
                                arguments_delta=fn_args,
                                index=tc_index,
                            )
                        )
                elif existing is not None and fn_args:
                    existing.arguments_buffer += fn_args
                    out.append(
                        ToolCallDelta(
                            id=existing.call_id,
                            arguments_delta=fn_args,
                            index=tc_index,
                        )
                    )

        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason is not None:
            for tc_index in sorted(state.tool_calls.keys()):
                in_progress = state.tool_calls[tc_index]
                try:
                    parsed = json.loads(in_progress.arguments_buffer or "{}")
                except json.JSONDecodeError:
                    parsed = {}
                out.append(
                    ToolCallEnd(
                        id=in_progress.call_id,
                        arguments=parsed,
                        index=in_progress.index,
                    )
                )
            state.tool_calls.clear()

            usage_event = _build_usage(getattr(chunk, "usage", None))
            if usage_event is not None:
                out.append(usage_event)

            out.append(
                Done(
                    stop_reason=_map_finish_reason(finish_reason),
                    raw_reason=finish_reason,
                )
            )
            return out

    if not choices:
        usage_event = _build_usage(getattr(chunk, "usage", None))
        if usage_event is not None:
            out.append(usage_event)

    return out


class OpenChatLLM(LLM):
    """Streaming LLM adapter for the OpenAI Chat Completions API."""

    def __init__(self, provider: LLMProvider) -> None:
        if provider.provider != LLMProviderType.OPENCHAT:
            raise ConfigError(
                f"OpenChatLLM requires provider type OPENCHAT; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, OpenChatConfig):
            raise ConfigError(
                "OpenChatLLM requires OpenChatConfig in provider.config"
            )

        self._provider = provider
        self._config: OpenChatConfig = provider.config
        self._policy = _POLICY_BY_FLAVOR[provider.config.flavor]

        key_present = (
            provider.config.api_key is not None
            and bool(provider.config.api_key.get_secret_value())
        )
        if self._policy.require_api_key and not key_present:
            raise ConfigError(
                f"api_key is required for flavor={provider.config.flavor.value}"
            )

        self._client: AsyncOpenAI | None = None
        self._max_concurrency = provider.limits.max_concurrency

        logger.info(
            "OpenChat adapter initialized",
            extra={
                "provider_id": provider.id,
                "flavor": provider.config.flavor.value,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
            },
        )

    async def list_models(self) -> Iterable[str]:
        return [m.name for m in self._provider.models]

    def _get_client(self) -> AsyncOpenAI:
        """Construct the AsyncOpenAI client lazily on first use."""
        if self._client is None:
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
            "messages": _messages_to_chat(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
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
            request["tools"] = [_tool_to_chat(t) for t in tools]
        choice_value = _tool_choice_to_chat(tool_choice)
        if choice_value is not None:
            request["tool_choice"] = choice_value
        rf_param = _response_format_to_param(response_format)
        if rf_param is not None:
            request["response_format"] = rf_param
        request.update(_extract_extended_kwargs(extended))

        logger.info(
            "OpenChat stream starting",
            extra={
                "provider_id": self._provider.id,
                "model": model,
                "message_count": len(messages),
                "tool_count": len(tools) if tools else 0,
            },
        )

        client = self._get_client()
        try:
            sdk_stream = await client.chat.completions.create(**request)
        except Exception as exc:
            err = classify_openai_exception(exc)
            logger.error(
                "OpenChat request failed before stream opened",
                extra={
                    "provider_id": self._provider.id,
                    "model": model,
                    "exception": type(exc).__name__,
                },
            )
            raise err from exc

        state = _StreamState()
        try:
            async for raw in sdk_stream:
                for event in _translate_chunk(raw, state):
                    yield event
        except Exception as exc:
            err = classify_openai_exception(exc)
            logger.error(
                "OpenChat stream aborted",
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
