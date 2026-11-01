"""Connect timeout on every adapter (Part 2 T50, AG-M16).

``connect_timeout_seconds`` did nothing on OpenChat and OpenRouter (``create()`` was not bounded) nor on Ollama and Gemini
(their SDKs send the request on the first iteration, which only the stall timeout bounded).
"""

from __future__ import annotations

import asyncio

import pytest

from primer.model.except_ import ProviderTimeoutError
from tests.llm.test_adapter_characterization import _ADAPTERS, _Wire, _collect, _provider, _route


class TestConnectTimeout:
    @pytest.mark.parametrize("kind", ["openchat", "openrouter", "ollama", "gemini", "anthropic", "openresponses"])
    async def test_a_slow_upstream_fails_with_connect_timeout_within_the_bound(self, monkeypatch, kind) -> None:
        async def slow(self, request):
            self.requests.append(request)
            await asyncio.sleep(3.0)  # a cold model load that never answers in time
            return self.http.Response(500)

        monkeypatch.setattr(_Wire, "handle", slow)
        wire = _route(monkeypatch, kind, [])
        prov = _provider(kind, connect_timeout_seconds=0.2, request_timeout_seconds=None, max_retries=0)
        with pytest.raises(ProviderTimeoutError) as caught:
            await asyncio.wait_for(_collect(_ADAPTERS[kind](prov), kind), 1.5)
        assert caught.value.code == "connect_timeout"
        assert len(wire.requests) == 1
