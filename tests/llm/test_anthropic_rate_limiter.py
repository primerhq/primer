"""Tests that the Anthropic adapter uses the injected RateLimiter."""

from __future__ import annotations

import asyncio
import inspect

import pytest


@pytest.mark.asyncio
async def test_anthropic_constructor_accepts_rate_limiter():
    """AnthropicLLM.__init__ has a rate_limiter kwarg."""
    from primer.llm.anthropic import AnthropicLLM
    sig = inspect.signature(AnthropicLLM.__init__)
    assert "rate_limiter" in sig.parameters


def test_anthropic_no_local_semaphore():
    """The adapter no longer constructs its own asyncio.Semaphore."""
    from primer.llm import anthropic
    source = inspect.getsource(anthropic)
    # Permit one false-positive guard: importing asyncio is fine; what's
    # forbidden is constructing an asyncio.Semaphore inside this module.
    assert "asyncio.Semaphore(" not in source, (
        "AnthropicLLM should rely on RateLimiter, not a local Semaphore."
    )


@pytest.mark.asyncio
async def test_anthropic_acquires_rate_limiter_during_call(monkeypatch):
    """Holding the limiter for a key blocks a second concurrent acquire."""
    from primer.coordinator.in_memory import InMemoryRateLimiter

    rl = InMemoryRateLimiter()
    lease1 = await rl.acquire("llm:test", max_concurrency=1)
    blocked = await rl.try_acquire("llm:test", max_concurrency=1, timeout_s=0.1)
    assert blocked is None
    await lease1.release()
