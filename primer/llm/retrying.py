"""An :class:`LLM` decorator that replays transport failures.

Applied once, in the provider registry's factory, so every adapter gets
retries without each one reimplementing them. Deliberately NOT applied to
the aggregated provider: that one already fails over across its members,
and wrapping it would retry the whole pool before trying the next member.
Its members are ordinary providers resolved through the same registry, so
they are each wrapped individually -- which is the order you want (retry
this member, then move on).

The retry policy itself lives in :mod:`primer.llm._retry`; this class is
just the plumbing that binds it to one provider row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from primer.int.llm import LLM
from primer.llm._retry import stream_with_retry
from primer.model.chat import Message, StreamEvent, Tool, ToolChoice
from primer.model.provider import LLMProvider


class RetryingLLM(LLM):
    """Wrap an :class:`LLM`, replaying streams that fail in transport."""

    def __init__(self, inner: LLM, provider: LLMProvider) -> None:
        self._inner = inner
        self._provider = provider
        limits = provider.limits
        self._max_retries = getattr(limits, "max_retries", 0)
        self._base = getattr(limits, "retry_backoff_seconds", 0.5)
        self._cap = getattr(limits, "retry_backoff_max_seconds", 8.0)

    @property
    def inner(self) -> LLM:
        """The wrapped adapter. Tests and the registry reach through this."""
        return self._inner

    def stream(  # type: ignore[override]
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
    ) -> AsyncIterator[StreamEvent]:
        if self._max_retries <= 0:
            return self._inner.stream(
                model=model, messages=messages, temperature=temperature,
                top_p=top_p, max_output_tokens=max_output_tokens, stop=stop,
                response_format=response_format, tools=tools,
                tool_choice=tool_choice, extended=extended,
            )

        def _open() -> AsyncIterator[StreamEvent]:
            # A fresh iterator per attempt: a retry re-opens the call rather
            # than resuming the exhausted one.
            return self._inner.stream(
                model=model, messages=messages, temperature=temperature,
                top_p=top_p, max_output_tokens=max_output_tokens, stop=stop,
                response_format=response_format, tools=tools,
                tool_choice=tool_choice, extended=extended,
            )

        return stream_with_retry(
            _open,
            provider_id=self._provider.id,
            provider_kind=self._provider.provider.value,
            max_retries=self._max_retries,
            base_backoff_seconds=self._base,
            max_backoff_seconds=self._cap,
        )

    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[Tool] | None = None,
    ) -> int:
        # Not retried: it is a best-effort estimate on the compaction hot
        # path that already falls back to a char heuristic on failure.
        return await self._inner.count_tokens(
            model=model, messages=messages, tools=tools,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()

    def __getattr__(self, item: str) -> Any:
        # Forward adapter-specific attributes tests and callers reach for
        # (_policy, _config, ...). Only consulted for names this class does
        # not define, so it never shadows the LLM surface above.
        return getattr(self._inner, item)


__all__ = ["RetryingLLM"]
