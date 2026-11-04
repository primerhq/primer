"""CV-2: the OpenAI embedder's SDK client follows the row's retry and timeout policy, not the SDK defaults (2 retries, 600 s)."""

from __future__ import annotations

import httpx

from primer.embedder.openai import OpenAIEmbedder
from primer.model.provider import EmbeddingProvider

KEY = "sk-live-SECRET-0123456789"


def _row(**limits) -> EmbeddingProvider:
    return EmbeddingProvider(
        id="e1", provider="openai", models=[{"name": "m"}],
        config={"url": "http://emb.internal:9000/v1", "api_key": KEY, "flavor": "other"},
        limits={"max_concurrency": 1, **limits},
    )


def test_the_client_retries_as_the_row_says():
    assert OpenAIEmbedder(_row(max_retries=0))._get_client().max_retries == 0
    assert OpenAIEmbedder(_row(max_retries=5))._get_client().max_retries == 5
    assert OpenAIEmbedder(_row())._get_client().max_retries == 2, "the row's default (2) is still 2, now as the row's own value"


def test_the_client_reads_for_the_rows_request_timeout():
    timeout = OpenAIEmbedder(_row(request_timeout_seconds=20))._get_client().timeout
    assert isinstance(timeout, httpx.Timeout) and timeout.read == 20
    assert OpenAIEmbedder(_row())._get_client().timeout.read == 300.0, "the row's default, not the SDK's 600 s"
    # D-A3-04: disabling the bound is a write only while a generation
    # deadline stays on, so the total budget carries it (the embedder
    # client never reads it).
    assert OpenAIEmbedder(_row(request_timeout_seconds=None, total_timeout_seconds=300.0))._get_client().timeout.read is None, "None disables the bound, as the limits document"


def test_the_connect_phase_follows_the_rows_connect_timeout():
    assert OpenAIEmbedder(_row(connect_timeout_seconds=7))._get_client().timeout.connect == 7
