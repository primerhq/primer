"""Anthropic ``model_context_window_exceeded`` is a max-tokens stop (Part 2 T51, AG-L10).

It mapped to ``other``, which ends an interactive session as completed; the model stopped because it ran out of room,
which is what ``max_tokens`` says.
"""

from __future__ import annotations

import primer.llm.anthropic as anthropic_mod
from primer.model.chat import Done
from tests.llm.test_adapter_characterization import ANTHROPIC_SSE, _OK, _collect, _provider, _route


def test_the_stop_reason_maps_to_max_tokens() -> None:
    assert anthropic_mod._map_stop_reason("model_context_window_exceeded", anthropic_mod._StreamState()) == "max_tokens"


async def test_the_adapter_reports_max_tokens(monkeypatch) -> None:
    body = ANTHROPIC_SSE.replace(b'"stop_reason": "tool_use"', b'"stop_reason": "model_context_window_exceeded"')
    _route(monkeypatch, "anthropic", [(200, body, _OK)])
    events = await _collect(anthropic_mod.AnthropicLLM(_provider("anthropic")), "anthropic")
    assert events[-1] == Done(stop_reason="max_tokens", raw_reason="model_context_window_exceeded")
