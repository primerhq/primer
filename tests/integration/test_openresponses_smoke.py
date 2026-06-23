"""Gated integration smoke tests for the OpenResponses adapter.

These are NOT run in normal pytest invocations because they each have a
``skipif`` gate. Enable by setting ``OPENAI_API_KEY`` (real OpenAI) or
running LM Studio locally on the default port (LM Studio).
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import cast
from urllib.parse import urlparse

import pytest
from pydantic import HttpUrl, SecretStr

from primer.llm.openresponses import OpenResponsesLLM
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
)
from primer.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenResponsesConfig,
    OpenResponsesFlavor,
)


# LM Studio host comes from the environment so no machine-specific address is
# baked into the repo (default: local instance).
_LMSTUDIO_URL = os.environ.get(
    "PRIMER_E2E_LMSTUDIO_URL", "http://localhost:8080"
).rstrip("/")
_parsed = urlparse(_LMSTUDIO_URL)
_LMSTUDIO_HOST = _parsed.hostname or "localhost"
_LMSTUDIO_PORT = _parsed.port or 8080


def _lmstudio_port_open(
    host: str = _LMSTUDIO_HOST, port: int = _LMSTUDIO_PORT
) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
        return True
    except (OSError, socket.timeout):
        return False
    finally:
        sock.close()


_LMSTUDIO_API_KEY = os.environ.get("PRIMER_E2E_LMSTUDIO_TOKEN", "")


def _lmstudio_has_model(
    host: str = _LMSTUDIO_HOST, port: int = _LMSTUDIO_PORT
) -> bool:
    """Return True iff LM Studio is reachable AND has at least one model loaded.

    Fast pre-check on the TCP port to avoid paying an HTTP timeout when the
    port is closed, then a quick GET on LM Studio's native REST endpoint
    ``/api/v0/models`` (which exposes a per-model ``state`` field) to confirm
    that at least one model has ``state == "loaded"``.

    Note: the OpenAI-compatible ``/v1/models`` endpoint lists every *installed*
    model regardless of whether it is loaded into memory, so it cannot be used
    to gate this test. ``/api/v0/models`` is the authoritative signal.
    """
    if not _LMSTUDIO_API_KEY:
        return False
    if not _lmstudio_port_open(host, port):
        return False
    url = f"http://{host}:{port}/api/v0/models"
    req = urllib.request.Request(  # noqa: S310
        url, headers={"Authorization": f"Bearer {_LMSTUDIO_API_KEY}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:  # noqa: S310
            if resp.status < 200 or resp.status >= 300:
                return False
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    if isinstance(payload, dict):
        data = payload.get("data")
    elif isinstance(payload, list):
        data = payload
    else:
        data = None
    if not isinstance(data, list):
        return False
    return any(
        isinstance(m, dict) and m.get("state") == "loaded" for m in data
    )


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
    not _lmstudio_has_model(),
    reason="PRIMER_E2E_LMSTUDIO_TOKEN unset, LM Studio unreachable, or no model loaded",
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
            url=HttpUrl(f"{_LMSTUDIO_URL}/v1/"),
            api_key=SecretStr(_LMSTUDIO_API_KEY),
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
