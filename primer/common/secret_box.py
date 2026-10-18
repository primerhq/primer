"""Sealing a secret value for storage: Fernet over a key derived from one piece of key material (plan 07, Task 4; design 12.6).

The MCP OAuth tokens and client secrets primer stores are sealed with a :class:`SecretBox`, so the database holds ciphertext only. The key material is
``PRIMER_ENCRYPTION_KEY``, or the session secret when that is not set (see :func:`primer.auth.secret.resolve_secret_box`). Every server process derives
the same key from the same material, so a value one process sealed another opens. Changing the material makes every sealed value unreadable
(:class:`SecretBoxError`); the owner of the value connects it again.

Format. A sealed value is ``"v1." + <Fernet token>``. The key is HKDF-SHA256 (32 bytes, salt ``primer-secret-box-v1``, info ``primer/mcp-oauth``) over the
UTF-8 key material, base64url-encoded as Fernet wants it. The derivation is part of the on-disk format: change the salt, the info or the framing and every
stored value stops opening, so a new derivation is a new prefix (``v2.``), and ``tests/common/test_secret_box.py`` pins a value sealed by an earlier build.

Fernet is AES-128-CBC with an HMAC-SHA256 tag and a random IV per value, so sealing the same text twice gives two different values and a damaged or
foreign value fails the tag check instead of decrypting to garbage.
"""

from __future__ import annotations

import base64
import re

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = ["SecretBox", "SecretBoxError", "is_sealed"]

_SALT = b"primer-secret-box-v1"
_INFO = b"primer/mcp-oauth"
_PREFIX = "v1."
# A Fernet token is url-safe base64 of: version byte 0x80 (so it always starts "gAAAA"), an 8-byte timestamp, a 16-byte IV, at least one AES block and a 32-byte tag (73 bytes, about 100 characters, at least).
_SEALED_SHAPE = re.compile(r"v1\.gAAAA[A-Za-z0-9_-]{90,}={0,2}")


def is_sealed(value: str) -> bool:
    """True when ``value`` has the SHAPE :meth:`SecretBox.encrypt` gives: the ``v1.`` prefix and a Fernet token. It opens nothing and says nothing about the key.

    A tripwire for a place that must only ever hold ciphertext (a stored token): plaintext handed to it is caught before it is written, instead of being stored and
    found later. It is not a proof: only :meth:`SecretBox.decrypt` shows that a value really opens.
    """
    return isinstance(value, str) and _SEALED_SHAPE.fullmatch(value) is not None


class SecretBoxError(Exception):
    """A sealed value could not be opened: another key sealed it, it was damaged, or it was never sealed by a :class:`SecretBox`.

    The message is fixed and names neither the plaintext nor the sealed value.
    """


class SecretBox:
    """Seals and opens strings with one derived key. Build it with :meth:`from_key_material`; it holds no state besides the key and is safe to share."""

    def __init__(self, fernet: Fernet) -> None:
        self._fernet = fernet

    @classmethod
    def from_key_material(cls, material: str) -> SecretBox:
        """The box for ``material``. Two boxes built from the same text open each other's values. ``ValueError`` for empty material."""
        if not material:
            raise ValueError("key material must not be empty")
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(material.encode("utf-8"))
        return cls(Fernet(base64.urlsafe_b64encode(derived)))

    def encrypt(self, plaintext: str) -> str:
        """``plaintext`` sealed: ``"v1."`` and a Fernet token. Different every time it is called."""
        return _PREFIX + self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """The text :meth:`encrypt` sealed. Raises :class:`SecretBoxError` for anything else: another key, a damaged value, a value without the prefix."""
        if not isinstance(token, str) or not token.startswith(_PREFIX):
            raise SecretBoxError("the value was not sealed by primer")
        try:
            return self._fernet.decrypt(token[len(_PREFIX):].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            raise SecretBoxError("the sealed value cannot be opened with the current encryption key, or it is damaged") from None
