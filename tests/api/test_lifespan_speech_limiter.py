"""The lifespan binds the coordinator's shared RateLimiter to the speech registries, so STT/TTS ``max_concurrency`` is global in distributed mode (not per-adapter)."""

from __future__ import annotations

import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.model.scheduler import RuntimeMode

from tests.api.conftest import _FakeStorageProvider


@pytest.mark.asyncio
async def test_startup_binds_the_shared_limiter_to_both_speech_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _FakeStorageProvider()
    monkeypatch.setattr("primer.api.app._build_storage_provider", lambda _cfg: storage)
    seen: dict[str, list] = {"stt": [], "tts": []}

    def spy(kind: str):
        def factory(row, *, rate_limiter=None):
            seen[kind].append(rate_limiter)
            return object()

        return factory

    monkeypatch.setattr("primer.api.registries.speech_registry.default_stt_factory", spy("stt"))
    monkeypatch.setattr("primer.api.registries.speech_registry.default_tts_factory", spy("tts"))
    app = create_app(AppConfig(runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None))
    async with app.router.lifespan_context(app):
        shared = app.state.coordinator.rate_limiter
        app.state.stt_registry._factory(object())  # noqa: SLF001 -- the factory a get() would build an adapter with
        app.state.tts_registry._factory(object())  # noqa: SLF001
    assert seen["stt"] == [shared]
    assert seen["tts"] == [shared]
