"""OpenRouter LLM adapter.

OpenRouter (https://openrouter.ai) is a unified gateway to 300+ LLMs
exposing a drop-in OpenAI-compatible /chat/completions endpoint. This
adapter wraps the openai Python SDK with three OpenRouter-specific
defaults:

1. A fixed ``base_url`` pointing at https://openrouter.ai/api/v1.
2. Optional ``X-Title`` and ``HTTP-Referer`` headers from the config
   for OpenRouter's app attribution surface.
3. :meth:`OpenRouterLLM.list_models` returns the configured
   ``LLMProvider.models`` list verbatim; the catalogue is fetched only
   by the discovery REST route (see :func:`_discover_openrouter_models`
   below), not at every agent dispatch.

Token counts are approximate. :meth:`OpenRouterLLM.count_tokens` falls
back to ``tiktoken``'s ``cl100k_base`` (same as :class:`OpenChatLLM`).
OpenRouter routes to many upstream providers whose native tokenisers
differ; the counts are used in primer for context-window warning
banners, not for billing, so the approximation is acceptable.

Spec: docs/superpowers/specs/2026-06-04-openrouter-llm-provider-design.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from primer.common.openai_errors import classify_openai_exception
from primer.int.coordinator import RateLimiter
from primer.int.llm import LLM
from primer.llm._timeout import _iter_with_timeout
from primer.llm._openai_compat import (
    _build_sampling_params,
    _extract_extended_kwargs,
    _messages_to_chat,
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
    OpenRouterConfig,
)
from primer.observability import tracing as _tracing


logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _attribution_headers(config: OpenRouterConfig) -> dict[str, str]:
    """Build the OpenRouter attribution header dict from a config.

    Returns an empty dict when neither attribution field is set so the
    caller can pass ``headers or None`` to clients that treat empty
    dicts as "send no extra headers".
    """
    headers: dict[str, str] = {}
    if config.app_name:
        headers["X-Title"] = config.app_name
    if config.app_url is not None:
        headers["HTTP-Referer"] = str(config.app_url)
    return headers


class OpenRouterLLM(LLM):
    """Streaming LLM adapter for OpenRouter (OpenAI-compatible upstream)."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        rate_limiter: RateLimiter | None = None,
        trace_llm_io: bool = False,
    ) -> None:
        if provider.provider != LLMProviderType.OPENROUTER:
            raise ConfigError(
                f"OpenRouterLLM requires provider type OPENROUTER; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, OpenRouterConfig):
            raise ConfigError(
                "OpenRouterLLM requires OpenRouterConfig in provider.config"
            )

        self._provider = provider
        self._config: OpenRouterConfig = provider.config
        self._client: AsyncOpenAI | None = None
        if rate_limiter is None:
            from primer.coordinator.in_memory import InMemoryRateLimiter
            rate_limiter = InMemoryRateLimiter()
        self._rate_limiter = rate_limiter
        self._rate_limit_key = f"llm:{provider.id}"
        self._max_concurrency = provider.limits.max_concurrency
        self._request_timeout_seconds = provider.limits.request_timeout_seconds
        self._trace_llm_io = trace_llm_io

        logger.info(
            "OpenRouter adapter initialized",
            extra={
                "provider_id": provider.id,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
                "request_timeout_seconds": provider.limits.request_timeout_seconds,
                "app_name_set": provider.config.app_name is not None,
                "app_url_set": provider.config.app_url is not None,
            },
        )

    def _get_client(self) -> AsyncOpenAI:
        """Construct the AsyncOpenAI client lazily on first use."""
        if self._client is None:
            headers = _attribution_headers(self._config)
            self._client = AsyncOpenAI(
                api_key=self._config.api_key.get_secret_value(),
                base_url=OPENROUTER_BASE_URL,
                default_headers=headers or None,
            )
        return self._client

    async def list_models(self) -> Iterable[str]:
        """Return the configured model slugs verbatim, sorted + deduplicated.

        Operator-typed slugs (Add-by-id surface in the UI) may not appear
        in OpenRouter's live catalogue. The agent picker must not lose
        models the operator deliberately added, so this method returns
        only what the provider config declares without hitting upstream.
        """
        return sorted({m.name for m in self._provider.models})

    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[Tool] | None = None,
    ) -> int:
        """Approximate via tiktoken cl100k_base; same path OpenChatLLM uses.

        Counts are approximate for non-OpenAI upstreams; used by primer
        for context-window warning banners, not for billing.
        """
        from primer.llm._tokenizer.openai import count_tokens_openai
        return count_tokens_openai(model=model, messages=messages, tools=tools)

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
            "OpenRouter stream starting",
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
                        "OpenRouter request failed before stream opened",
                        extra={
                            "provider_id": self._provider.id,
                            "model": model,
                            "exception": type(exc).__name__,
                        },
                    )
                    raise err from exc

                state = _StreamState()
                try:
                    async for raw in _iter_with_timeout(
                        sdk_stream, self._request_timeout_seconds
                    ):
                        for event in _translate_chunk(raw, state):
                            yield event
                except TimeoutError as exc:
                    from primer.model.except_ import ProviderTimeoutError
                    timeout_val = self._request_timeout_seconds
                    logger.error(
                        "OpenRouter stream timed out (no event in %.1f s)",
                        timeout_val,
                        extra={
                            "provider_id": self._provider.id,
                            "model": model,
                        },
                    )
                    raise ProviderTimeoutError(
                        f"OpenRouter stream stalled: no event received within "
                        f"{timeout_val} s (provider_id={self._provider.id!r}, "
                        f"model={model!r})",
                        code="stream_timeout",
                    ) from exc
                except Exception as exc:
                    err = classify_openai_exception(exc)
                    logger.error(
                        "OpenRouter stream aborted",
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

    async def aclose(self) -> None:
        """Close the openai SDK client (httpx pool). Idempotent.

        :class:`OpenChatLLM` does not close its client today; this
        adapter explicitly does so the cached registry entry releases
        its connection pool when invalidated.
        """
        if self._client is not None:
            await self._client.close()
            self._client = None


async def _discover_openrouter_models(
    draft_config: OpenRouterConfig,
) -> list[dict[str, Any]]:
    """Probe OpenRouter's ``GET /api/v1/models`` with the draft credentials.

    Used by the discovery REST route powering the UI's *Fetch Models*
    button: the operator types an API key into the new-provider form,
    the route calls this helper, and the response populates the model
    picker.

    Returns a list of dicts (one per upstream model) with:

    - ``id``: the slug (e.g. ``anthropic/claude-3.5-sonnet``).
    - ``name``: human-readable name; defaults to ``id`` if upstream omits.
    - ``context_length``: context-window cap; ``None`` if upstream omits.
    - ``input_price_per_million``: prompt cost per million tokens as a
      string (OpenRouter's native shape); ``None`` if missing.
    - ``output_price_per_million``: completion cost per million tokens
      as a string; ``None`` if missing.
    - ``modality``: e.g. ``"text"``, ``"text+image"``; defaults to
      ``"text"`` (spec §6.2).

    Uses a plain :class:`httpx.AsyncClient` rather than the openai SDK's
    raw-GET path because the openai SDK requires a ``cast_to`` type and
    can strip fields its strict typer does not recognise; the catalogue
    payload carries OpenRouter-specific fields the SDK has never heard
    of (``architecture``, ``pricing``, ``per_request_limits``, ...).
    """
    headers = {
        "Authorization": f"Bearer {draft_config.api_key.get_secret_value()}",
        **_attribution_headers(draft_config),
    }
    async with httpx.AsyncClient(
        base_url=OPENROUTER_BASE_URL, headers=headers, timeout=30.0,
    ) as client:
        response = await client.get("/models")
        response.raise_for_status()
        payload = response.json()

    out: list[dict[str, Any]] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("id")
        if not slug:
            continue
        pricing = entry.get("pricing") or {}
        architecture = entry.get("architecture") or {}
        out.append({
            "id": slug,
            "name": entry.get("name") or slug,
            "context_length": entry.get("context_length"),
            "input_price_per_million": pricing.get("prompt"),
            "output_price_per_million": pricing.get("completion"),
            "modality": architecture.get("modality") or "text",
        })
    return out
