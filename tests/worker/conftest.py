"""Worker test fixtures.

The chat integration test needs an ``app`` fixture that starts a real
worker pool (chat claim loop included) with a fake LLM wired in.  We
define that here rather than pulling it from tests/api/conftest.py so
the API tests keep their simpler ``worker_pool=None`` setup.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry

from tests.conftest import _FakeStorageProvider


@pytest_asyncio.fixture
async def app(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
    fake_llm,
) -> AsyncIterator[FastAPI]:
    """Worker-suite app fixture: starts a real WorkerPool + chat claim loop
    with ``fake_llm`` wired through the provider registry.  Used by
    ``test_chat_claim_loop.py`` and any future integration tests that need
    the worker running in-process."""

    # Wire the fake LLM through the provider registry so the chat claim
    # loop can resolve LLM providers without a real endpoint.
    async def _get_llm(_pid: str):
        return fake_llm

    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]

    _app = create_test_app(
        storage_provider=fake_storage_provider,  # type: ignore[arg-type]
        provider_registry=fake_provider_registry,
        start_chat_worker=True,
    )
    forwarder = await _app.state.start_chat_tick_forwarder()
    await _app.state.start_worker_pool()
    try:
        yield _app
    finally:
        try:
            await _app.state.stop_worker_pool()
        except Exception:
            pass
        forwarder.cancel()
        try:
            await forwarder
        except asyncio.CancelledError:
            pass
