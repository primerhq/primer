"""Plaintext token minting + hashing for ApiToken.

Spec §4 — sha256(plaintext) is the stored credential. Plaintext is shown
once at creation and never persisted.
"""

from __future__ import annotations

import hashlib
import secrets


PLAINTEXT_PREFIX = "primer_pat_"
_RANDOM_BYTES = 32


def mint_plaintext() -> str:
    """Fresh URL-safe random token: prefix + 256 bits of entropy."""
    return PLAINTEXT_PREFIX + secrets.token_urlsafe(_RANDOM_BYTES)


def hash_token(plaintext: str) -> str:
    """sha256 hex of plaintext UTF-8 bytes."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def extract_prefix(plaintext: str) -> str:
    """First 8 chars of plaintext for UI disambiguation."""
    return plaintext[:8]


__all__ = ["PLAINTEXT_PREFIX", "mint_plaintext", "hash_token", "extract_prefix"]
