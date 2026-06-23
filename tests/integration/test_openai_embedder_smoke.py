"""Gated integration smoke tests for the OpenAI embedder adapter.

These are NOT run in normal pytest invocations because they each have
a ``skipif`` gate. Enable by setting ``OPENAI_API_KEY`` (real OpenAI)
or running LM Studio locally on the default port AND setting
``LMSTUDIO_EMBED_MODEL`` (LM Studio).
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest
from pydantic import HttpUrl, SecretStr

from primer.embedder.openai import OpenAIEmbedder
from primer.model.embedding import EmbedResponse, TextPart
from primer.model.provider import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    Limits,
    OpenAIConfig,
    OpenAIEmbeddingFlavor,
)

# LM Studio host comes from the environment so no machine-specific address is
# baked into the repo (default: local instance). The trailing /v1/ is the
# OpenAI-compatible API base.
_LMSTUDIO_URL = os.environ.get(
    "PRIMER_E2E_LMSTUDIO_URL", "http://localhost:8080"
).rstrip("/")
_parsed = urlparse(_LMSTUDIO_URL)
_LMSTUDIO_HOST = _parsed.hostname or "localhost"
_LMSTUDIO_PORT = _parsed.port or 8080


def _lmstudio_reachable(
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


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)
async def test_real_openai_smoke() -> None:
    provider = EmbeddingProvider(
        id="real-openai",
        provider=EmbeddingProviderType.OPENAI,
        models=[EmbeddingModel(name="text-embedding-3-small")],
        config=OpenAIConfig(
            url=HttpUrl("https://api.openai.com/v1/"),
            api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
            flavor=OpenAIEmbeddingFlavor.OPENAI,
        ),
        limits=Limits(max_concurrency=2),
    )
    embedder = OpenAIEmbedder(provider)
    out: EmbedResponse = await embedder.embed(
        model="text-embedding-3-small",
        inputs=[TextPart(text="hello")],
    )
    assert len(out.embeddings) == 1
    assert len(out.embeddings[0].vector) > 0
    assert out.usage is not None
    assert out.usage.input_tokens is not None and out.usage.input_tokens > 0


@pytest.mark.skipif(
    not (
        _lmstudio_reachable()
        and os.environ.get("LMSTUDIO_EMBED_MODEL")
        and os.environ.get("PRIMER_E2E_LMSTUDIO_TOKEN")
    ),
    reason="LM Studio not reachable, LMSTUDIO_EMBED_MODEL unset, or PRIMER_E2E_LMSTUDIO_TOKEN unset",
)
async def test_lmstudio_smoke() -> None:
    # Embedding model selection on LM Studio varies by what the user
    # has loaded; no sensible default exists, so we require the env var.
    model_name = os.environ["LMSTUDIO_EMBED_MODEL"]
    provider = EmbeddingProvider(
        id="lmstudio-local",
        provider=EmbeddingProviderType.OPENAI,
        models=[EmbeddingModel(name=model_name)],
        config=OpenAIConfig(
            url=HttpUrl(f"{_LMSTUDIO_URL}/v1/"),
            api_key=SecretStr(os.environ["PRIMER_E2E_LMSTUDIO_TOKEN"]),
            flavor=OpenAIEmbeddingFlavor.LMSTUDIO,
        ),
        limits=Limits(max_concurrency=1),
    )
    embedder = OpenAIEmbedder(provider)
    out: EmbedResponse = await embedder.embed(
        model=model_name,
        inputs=[TextPart(text="hello")],
    )
    assert len(out.embeddings) == 1
    assert len(out.embeddings[0].vector) > 0
