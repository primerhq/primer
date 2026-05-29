"""Gated integration smoke tests for the OpenChat adapter.

Both tests are skipped by default — they each have an env / reachability
gate. Enable by setting ``OPENAI_API_KEY`` (real OpenAI) or running LM
Studio on the default port (LM Studio).
"""

from __future__ import annotations

import os
import socket
from typing import cast

import pytest
from pydantic import HttpUrl, SecretStr

from primer.llm.openchat import OpenChatLLM
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    Usage,
)
from primer.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)


def _lmstudio_reachable(host: str = "localhost", port: int = 1234) -> bool:
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
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)
async def test_real_openai_smoke() -> None:
    provider = LLMProvider(
        id="real-openai-chat",
        provider=LLMProviderType.OPENCHAT,
        models=[LLMModel(name="gpt-4o-mini", context_length=128_000)],
        config=OpenChatConfig(
            url=HttpUrl("https://api.openai.com/v1/"),
            api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
            flavor=OpenChatFlavor.OPENAI,
        ),
        limits=Limits(max_concurrency=2),
    )
    llm = OpenChatLLM(provider)
    events: list[StreamEvent] = []
    async for event in llm.stream(
        model="gpt-4o-mini",
        messages=[
            Message(
                role="user",
                parts=[TextPart(text="Reply with the single word 'pong'.")],
            )
        ],
        max_output_tokens=10,
    ):
        events.append(cast(StreamEvent, event))
    assert any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], Done)
    assert events[-1].stop_reason in {"stop", "max_tokens"}
    usage_events = [e for e in events if isinstance(e, Usage)]
    assert usage_events and usage_events[0].input_tokens > 0


@pytest.mark.skipif(
    not _lmstudio_reachable() or not os.environ.get("PRIMER_E2E_LMSTUDIO_TOKEN"),
    reason="LM Studio not reachable on localhost:1234 or PRIMER_E2E_LMSTUDIO_TOKEN not set",
)
async def test_lmstudio_smoke() -> None:
    model_name = os.environ.get("LMSTUDIO_MODEL", "local-model")
    provider = LLMProvider(
        id="lmstudio-local-chat",
        provider=LLMProviderType.OPENCHAT,
        models=[LLMModel(name=model_name, context_length=8192)],
        config=OpenChatConfig(
            url=HttpUrl("http://localhost:1234/v1/"),
            api_key=SecretStr(os.environ["PRIMER_E2E_LMSTUDIO_TOKEN"]),
            flavor=OpenChatFlavor.LMSTUDIO,
        ),
        limits=Limits(max_concurrency=1),
    )
    llm = OpenChatLLM(provider)
    events: list[StreamEvent] = []
    async for event in llm.stream(
        model=model_name,
        messages=[
            Message(role="user", parts=[TextPart(text="Say 'hello' and nothing else.")])
        ],
        max_output_tokens=10,
    ):
        events.append(cast(StreamEvent, event))
    assert any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], Done)
    assert events[-1].stop_reason in {"stop", "max_tokens"}
