"""OpenChat LLM adapter — wraps the OpenAI Chat Completions API.

Subclasses :class:`primer.int.LLM` and translates the universal chat
interface (:mod:`primer.model.chat`) onto the legacy OpenAI
``/v1/chat/completions`` wire format. Targets real OpenAI, LM Studio,
Ollama's OpenAI shim, vLLM, and any other compatible server via the
:class:`OpenChatFlavor` discriminator on the provider config.

Parallel structure to :mod:`primer.llm.openresponses`. Sampling-knob
translation lives in :mod:`primer.llm._openai_common`; request shaping
and SSE-chunk translation live in :mod:`primer.llm._openai_compat`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel

from primer.common.openai_errors import classify_openai_exception
from primer.int.coordinator import RateLimiter
from primer.int.llm import LLM
from primer.llm._openai_compat import (
    _build_sampling_params,
    _build_usage,
    _extract_extended_kwargs,
    _map_finish_reason,
    _messages_to_chat,
    _part_to_content,
    _response_format_to_param,
    _StreamState,
    _tool_choice_to_chat,
    _tool_to_chat,
    _translate_chunk,
)
from primer.llm._trace import _serialize_messages
from primer.model.chat import (
    Error as ChatError,
    Message,
    Tool,
    ToolChoice,
)
from primer.model.except_ import ConfigError, ModelNotFoundError
from primer.model.provider import (
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)
from primer.observability import tracing as _tracing


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


class OpenChatLLM(LLM):
    """Streaming LLM adapter for the OpenAI Chat Completions API."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        rate_limiter: RateLimiter | None = None,
        trace_llm_io: bool = False,
    ) -> None:
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
        if rate_limiter is None:
            from primer.coordinator.in_memory import InMemoryRateLimiter
            rate_limiter = InMemoryRateLimiter()
        self._rate_limiter = rate_limiter
        self._rate_limit_key = f"llm:{provider.id}"
        self._max_concurrency = provider.limits.max_concurrency
        self._trace_llm_io = trace_llm_io

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
            async with await self._rate_limiter.acquire(
                self._rate_limit_key, max_concurrency=self._max_concurrency,
            ):
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
            _span.set_attribute(
                "llm.duration_ms",
                int((time.monotonic() - _t0) * 1000),
            )
