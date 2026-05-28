"""Instrumentation endpoints for distributed-mode test scenarios.

These routes are mounted ONLY when the ``MATRIX_ENABLE_TEST_ENDPOINTS``
environment variable is set to ``1``.  They are never included in the
production OpenAPI schema and must not be relied upon by application
code outside of ``tests/``.

Mount point: ``/v1/_test/*``
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request


router = APIRouter(tags=["_test"])


@router.post("/_test/acquire_rate_limit")
async def acquire_rate_limit(
    key: str,
    max_concurrency: int,
    sleep_ms: int,
    request: Request,
) -> dict[str, bool]:
    """Acquire a rate-limit lease, sleep, then release.

    Used by the S1 distributed scenario to measure peak concurrency
    under burst across processes.

    Query parameters
    ----------------
    key : str
        Rate-limiter key (e.g. ``"provider:some-id"``).
    max_concurrency : int
        Maximum concurrent holders allowed for *key*.
    sleep_ms : int
        How long to hold the lease (milliseconds).
    """
    coordinator = request.app.state.coordinator
    async with await coordinator.rate_limiter.acquire(
        key, max_concurrency=max_concurrency
    ):
        await asyncio.sleep(sleep_ms / 1000)
    return {"ok": True}


__all__ = ["router"]
