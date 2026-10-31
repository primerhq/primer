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
    """First 8 characters of the random part, for the console to tell tokens apart.

    Every plaintext starts with :data:`PLAINTEXT_PREFIX`, so its own first 8 characters were the same for every token (``primer_p``): a leaked token
    in a log could not be matched to the row to revoke (review of gaps-security, GS-43). Rows made before keep the prefix they stored.
    """
    body = plaintext[len(PLAINTEXT_PREFIX):] if plaintext.startswith(PLAINTEXT_PREFIX) else plaintext
    return body[:8]


__all__ = ["PLAINTEXT_PREFIX", "mint_plaintext", "hash_token", "extract_prefix"]
