"""Pinned identities of the tiktoken vocabularies primer counts tokens with.

Stdlib only, on purpose: ``scripts/bake_tokenizers.py`` loads this file by path
during the image build, before any of primer's dependencies exist.

tiktoken's own loader (``tiktoken.load.read_file_cached``) re-fetches over the
network, with no timeout, whenever its cached file is missing or fails the hash
check, no matter what ``TIKTOKEN_CACHE_DIR`` says. These pins are what lets
primer verify a file it chose, instead of trusting tiktoken to do so. They
mirror ``tiktoken_ext.openai_public``; ``tests/tooling/test_bake_tokenizers.py``
fails if an installed tiktoken disagrees, so a tiktoken bump cannot drift
silently.
"""

from __future__ import annotations

from typing import NamedTuple


class VocabPin(NamedTuple):
    encoding: str
    url: str
    sha256: str


PINS: tuple[VocabPin, ...] = (
    VocabPin(
        encoding="o200k_base",
        url="https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken",
        sha256="446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
    ),
    VocabPin(
        encoding="cl100k_base",
        url="https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken",
        sha256="223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    ),
)
