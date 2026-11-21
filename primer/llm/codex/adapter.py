"""Unregistered LLM foundation over an admitted executor-free bridge."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from primer.int.coordinator import RateLimiter
from primer.int.llm import LLM
from primer.llm.codex.bridge import ModelOnlyBridge
from primer.llm.codex.request import prepare_request
from primer.llm.codex.lifecycle import OwnedStream
from primer.llm.codex.boundary import BoundaryError
from primer.model.chat import Error, Message, StreamEvent, Tool, ToolChoice
from primer.model.except_ import TokenCounterUnavailable, UnsupportedContentError


class CodexFoundationLLM(LLM):
    """Not a configurable provider until the live boundary is independently qualified.

    Receiving a bridge is trusted dependency injection, not an authorization
    claim. No registry, enum, UI or credential-config entry enables this class.
    """
    def __init__(self, bridge: ModelOnlyBridge, *, provider_id: str,
                 rate_limiter: RateLimiter, max_concurrency: int = 1):
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._bridge = bridge
        self._limiter = rate_limiter
        self._key = f"llm:{provider_id}"
        self._concurrency = max_concurrency
        self._active: set[OwnedStream] = set()
        self._closed = False

    def stream(self, *, model: str, messages: list[Message],
                     temperature: float | None = None, top_p: float | None = None,
                     max_output_tokens: int | None = None, stop: list[str] | None = None,
                     response_format: type[BaseModel] | dict[str, Any] | None = None,
                     tools: list[Tool] | None = None, tool_choice: ToolChoice | None = None,
                     extended: dict[str, Any] | None = None) -> OwnedStream:
        if self._closed:
            raise BoundaryError("Codex adapter is closed")
        stream = OwnedStream(self._stream(
            model=model, messages=messages, temperature=temperature, top_p=top_p,
            max_output_tokens=max_output_tokens, stop=stop, response_format=response_format,
            tools=tools, tool_choice=tool_choice, extended=extended,
        ), on_close=self._active.discard)
        self._active.add(stream)
        return stream

    async def _stream(self, *, model: str, messages: list[Message],
                     temperature: float | None = None, top_p: float | None = None,
                     max_output_tokens: int | None = None, stop: list[str] | None = None,
                     response_format: type[BaseModel] | dict[str, Any] | None = None,
                     tools: list[Tool] | None = None, tool_choice: ToolChoice | None = None,
                     extended: dict[str, Any] | None = None) -> AsyncIterator[StreamEvent]:
        if temperature is not None or top_p is not None or stop or extended:
            raise UnsupportedContentError("Codex foundation does not yet support sampling or extended overrides")
        schema = response_format.model_json_schema() if isinstance(response_format, type) else response_format
        request = prepare_request(model, messages, tools, tool_choice=tool_choice,
                                  max_output_tokens=max_output_tokens, response_schema=schema)
        async with await self._limiter.acquire(self._key, max_concurrency=self._concurrency):
            stream = self._bridge.stream(request)
            try:
                async for event in stream:
                    yield event
            except Exception:
                # Raw policy/model errors may contain sensitive upstream data.
                # Detailed error classification is a later integration phase.
                yield Error(code="codex_boundary_error", message="Codex model step failed or was refused", fatal=True)
            finally:
                await stream.aclose()

    async def count_tokens(self, *, model: str, messages: list[Message], tools: list[Tool] | None = None) -> int:
        raise TokenCounterUnavailable("Codex native token counter is not available")

    async def aclose(self) -> None:
        self._closed = True
        if any(s._pending is asyncio.current_task() for s in self._active):
            raise BoundaryError("Codex owner cannot close inside its own advancement")
        results = await asyncio.gather(*(s.aclose() for s in tuple(self._active)), return_exceptions=True)
        try:
            await self._bridge.aclose()
        finally:
            for result in results:
                if isinstance(result, BaseException):
                    raise result
