"""Gated integration smoke for Ollama LLM.

Default-skip; enable by having Ollama reachable on localhost:11434
AND setting OLLAMA_MODEL.
"""

from __future__ import annotations

import os
import socket
from typing import cast

import pytest
from pydantic import HttpUrl

from matrix.llm.ollama import OllamaLLM
from matrix.model.chat import Done, Message, StreamEvent, TextDelta, TextPart
from matrix.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OllamaConfig,
)


def _ollama_reachable(host: str = "localhost", port: int = 11434) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
        return True
    except (OSError, socket.timeout):
        return False
    finally:
        sock.close()


@pytest.mark.skipif(
    not (_ollama_reachable() and os.environ.get("OLLAMA_MODEL")),
    reason="Ollama not reachable on localhost:11434 OR OLLAMA_MODEL not set",
)
async def test_real_ollama_smoke() -> None:
    model_name = os.environ["OLLAMA_MODEL"]
    provider = LLMProvider(
        id="ollama-real",
        provider=LLMProviderType.OLLAMA,
        models=[LLMModel(name=model_name, context_length=8192)],
        config=OllamaConfig(url=HttpUrl("http://localhost:11434")),
        limits=Limits(max_concurrency=2),
    )
    llm = OllamaLLM(provider)
    events: list[StreamEvent] = []
    async for event in llm.stream(
        model=model_name,
        messages=[Message(
            role="user",
            parts=[TextPart(text="Reply with the single word 'pong'.")],
        )],
        max_output_tokens=10,
    ):
        events.append(cast(StreamEvent, event))
    assert any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], Done)
