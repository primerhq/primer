"""Gated integration smoke tests for the OpenResponses adapter.

These are NOT run in normal pytest invocations because they each have a
``skipif`` gate. Enable by setting ``OPENAI_API_KEY`` (real OpenAI) or
running LM Studio locally on the default port (LM Studio).
"""

from __future__ import annotations

import os
import socket
from typing import cast

import pytest
from pydantic import HttpUrl, SecretStr

from matrix.llm.openresponses import OpenResponsesLLM
from matrix.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
)
from matrix.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenResponsesConfig,
    OpenResponsesFlavor,
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
        id="real-openai",
        provider=LLMProviderType.OPENRESPONSES,
        models=[LLMModel(name="gpt-4o-mini", context_length=128_000)],
        config=OpenResponsesConfig(
            url=HttpUrl("https://api.openai.com/v1/"),
            api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
            flavor=OpenResponsesFlavor.OPENAI,
        ),
        limits=Limits(max_concurrency=2),
    )
    llm = OpenResponsesLLM(provider)
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


@pytest.mark.skipif(
    not _lmstudio_reachable(),
    reason="LM Studio not reachable on localhost:1234",
)
async def test_lmstudio_smoke() -> None:
    # Pull whatever the user has loaded locally; LM Studio shows it
    # under /v1/models. We just need any model name LM Studio accepts.
    model_name = os.environ.get("LMSTUDIO_MODEL", "local-model")
    provider = LLMProvider(
        id="lmstudio-local",
        provider=LLMProviderType.OPENRESPONSES,
        models=[LLMModel(name=model_name, context_length=8192)],
        config=OpenResponsesConfig(
            url=HttpUrl("http://localhost:1234/v1/"),
            api_key=SecretStr(""),
            flavor=OpenResponsesFlavor.LMSTUDIO,
        ),
        limits=Limits(max_concurrency=1),
    )
    llm = OpenResponsesLLM(provider)
    events: list[StreamEvent] = []
    async for event in llm.stream(
        model=model_name,
        messages=[
            Message(
                role="user", parts=[TextPart(text="Say 'hello' and nothing else.")]
            )
        ],
        max_output_tokens=10,
    ):
        events.append(cast(StreamEvent, event))
    assert isinstance(events[-1], Done)
