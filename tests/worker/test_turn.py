"""Tests for matrix.worker.turn helpers (_CancelScope + retry classification)."""

from __future__ import annotations

import asyncio

import pytest

from matrix.model.except_ import TransientError
from matrix.worker.turn import (
    _CancelScope,
    classify_exception,
    compute_backoff,
)


async def test_cancel_scope_cancels_current_task():
    cancelled = False
    scope = _CancelScope()

    async def runner():
        nonlocal cancelled
        async with scope:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise

    task = asyncio.create_task(runner())
    await asyncio.sleep(0)
    scope.cancel("test")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled


def test_classify_exception_transient():
    assert classify_exception(TransientError("net blip")) == "transient"


def test_classify_exception_cancelled():
    assert classify_exception(asyncio.CancelledError()) == "cancelled"


def test_classify_exception_fatal():
    assert classify_exception(ValueError("bad")) == "fatal"


def test_compute_backoff_grows_exponentially():
    assert compute_backoff(attempt=1, base=2.0, cap=300.0) == 2.0
    assert compute_backoff(attempt=2, base=2.0, cap=300.0) == 4.0
    assert compute_backoff(attempt=3, base=2.0, cap=300.0) == 8.0


def test_compute_backoff_caps():
    assert compute_backoff(attempt=20, base=2.0, cap=300.0) == 300.0
