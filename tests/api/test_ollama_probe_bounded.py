"""GS-4: a silent Ollama server answers a timeout error instead of holding the probe for ever."""

from __future__ import annotations

import asyncio

import pytest

import primer.api.routers.providers as providers
from primer.model.except_ import BadRequestError


async def test_silent_ollama_answers_a_timeout_error(monkeypatch):
    monkeypatch.setattr(providers, "_OLLAMA_REQUEST_TIMEOUT_S", 0.3)
    monkeypatch.setattr(providers, "_OLLAMA_PROBE_TIMEOUT_S", 1.0)

    async def _silent(reader, writer):
        await asyncio.sleep(3600)

    server = await asyncio.start_server(_silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(BadRequestError):
            await asyncio.wait_for(providers._probe_llm_models("ollama", {"url": f"http://127.0.0.1:{port}"}), 5)
    finally:
        server.close()


async def test_responsive_ollama_still_lists_models():
    class _Model:
        model = "llama3"

    class _Resp:
        models = [_Model()]

    class _Client:
        async def list(self):
            return _Resp()

        async def show(self, name):
            return type("I", (), {"modelinfo": {"llama.context_length": 8192}})()

    assert await providers._ollama_models_of(_Client()) == {"models": [{"name": "llama3", "context_length": 8192}]}
