"""Gated integration smoke tests for the OpenAI embedder adapter.

These are NOT run in normal pytest invocations because they each have
a ``skipif`` gate. Enable by setting ``OPENAI_API_KEY`` (real OpenAI)
or running LM Studio locally on the default port AND setting
``LMSTUDIO_EMBED_MODEL`` (LM Studio).
"""

from __future__ import annotations

import os
import socket

import pytest
from pydantic import HttpUrl, SecretStr

from matrix.embedder.openai import OpenAIEmbedder
from matrix.model.embedding import EmbedResponse, TextPart
from matrix.model.provider import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    Limits,
    OpenAIConfig,
    OpenAIEmbeddingFlavor,
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
    not (_lmstudio_reachable() and os.environ.get("LMSTUDIO_EMBED_MODEL")),
    reason="LM Studio not reachable on localhost:1234 OR LMSTUDIO_EMBED_MODEL not set",
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
            url=HttpUrl("http://localhost:1234/v1/"),
            api_key=SecretStr(""),
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
