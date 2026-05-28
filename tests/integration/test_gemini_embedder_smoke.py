"""Gated integration smoke for Gemini Embedder.
Default-skip; enable by setting GEMINI_API_KEY.
"""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from primer.embedder.gemini import GeminiEmbedder
from primer.model.embedding import EmbedResponse, TextPart
from primer.model.provider import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    GoogleConfig,
    Limits,
)


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set",
)
async def test_real_gemini_embedder_smoke() -> None:
    provider = EmbeddingProvider(
        id="gemini-emb-real",
        provider=EmbeddingProviderType.GEMINI,
        models=[EmbeddingModel(name="text-embedding-004")],
        config=GoogleConfig(api_key=SecretStr(os.environ["GEMINI_API_KEY"])),
        limits=Limits(max_concurrency=2),
    )
    embedder = GeminiEmbedder(provider)
    out: EmbedResponse = await embedder.embed(
        model="text-embedding-004",
        inputs=[TextPart(text="hello")],
    )
    assert len(out.embeddings) == 1
    assert len(out.embeddings[0].vector) > 0
