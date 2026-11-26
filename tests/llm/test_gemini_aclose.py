"""Review 2026-11-23 finding 2.11: GeminiLLM.aclose must close the ASYNC client
pool (``client.aio``) that every stream call uses, not just the sync client.

The adapter streams through ``client.aio.models.generate_content_stream``;
closing only the sync client leaked the async httpx pool on shutdown.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.llm.gemini import GeminiLLM

from tests.llm.test_gemini import _make_provider


def _llm_with_client(client: MagicMock) -> GeminiLLM:
    llm = GeminiLLM(_make_provider())
    llm._client = client
    return llm


@pytest.mark.asyncio
async def test_aclose_closes_async_pool() -> None:
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.aclose = AsyncMock()
    client.close = MagicMock()
    llm = _llm_with_client(client)

    await llm.aclose()

    client.aio.aclose.assert_awaited_once_with()
    client.close.assert_called_once_with()
    assert llm._client is None


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.aclose = AsyncMock()
    client.close = MagicMock()
    llm = _llm_with_client(client)

    await llm.aclose()
    await llm.aclose()  # nothing left to close the second time

    client.aio.aclose.assert_awaited_once_with()
    client.close.assert_called_once_with()
    assert llm._client is None


@pytest.mark.asyncio
async def test_aclose_without_client_is_a_noop() -> None:
    llm = GeminiLLM(_make_provider())
    assert llm._client is None

    await llm.aclose()  # must not raise

    assert llm._client is None
