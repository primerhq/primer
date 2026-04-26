"""Gated integration smoke test for the HuggingFace embedder adapter.

Default-skip because the first run downloads ~80MB. Enable by setting
HUGGINGFACE_SMOKE=1.
"""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from matrix.embedder.huggingface import HuggingFaceEmbedder
from matrix.model.embedding import EmbedResponse, TextPart
from matrix.model.provider import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
    Limits,
)


@pytest.mark.skipif(
    os.environ.get("HUGGINGFACE_SMOKE") != "1",
    reason="HUGGINGFACE_SMOKE=1 not set",
)
async def test_real_huggingface_smoke() -> None:
    provider = EmbeddingProvider(
        id="hf-real",
        provider=EmbeddingProviderType.HUGGINGFACE,
        models=[EmbeddingModel(
            name="sentence-transformers/all-MiniLM-L6-v2",
            length=384,
        )],
        config=HuggingFaceConfig(token=SecretStr("")),
        limits=Limits(max_concurrency=1),
    )
    embedder = HuggingFaceEmbedder(provider)
    out: EmbedResponse = await embedder.embed(
        model="sentence-transformers/all-MiniLM-L6-v2",
        inputs=[TextPart(text="hello world")],
    )
    assert len(out.embeddings) == 1
    assert len(out.embeddings[0].vector) == 384
