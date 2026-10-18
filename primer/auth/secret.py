"""Resolve the session-signing secret, and the key that seals the MCP OAuth tokens.

Session secret priority:
1. ``AuthConfig.session_secret`` (loaded from ``PRIMER_SESSION_SECRET``
   env var). Operator-set; takes precedence.
2. ``system_state.session_secret`` column. Auto-generated on first
   need, persisted for restart durability.

If neither is set, this module generates a fresh 32-byte hex secret
and writes it back via :meth:`StorageProvider.set_session_secret`.

Encryption key (:func:`resolve_secret_box`): ``AppConfig.encryption_key``
(``PRIMER_ENCRYPTION_KEY``) when set, else the session secret above, with a
warning, because the session secret sits in the same database as the values
it would then protect.
"""

from __future__ import annotations

import logging
import secrets

from primer.api.config import AppConfig, AuthConfig
from primer.common.secret_box import SecretBox
from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConfigError

logger = logging.getLogger(__name__)

#: The shortest ``PRIMER_ENCRYPTION_KEY`` accepted: the key material is hashed into the real key, so its length is the whole of its strength.
MIN_ENCRYPTION_KEY_LENGTH = 16

SECRET_BOX_FALLBACK_WARNING = (
    "PRIMER_ENCRYPTION_KEY is not set: MCP OAuth tokens are encrypted with a key derived from the session secret, "
    "which anyone who can read the database can read too, so the tokens are only obfuscated. "
    "Set PRIMER_ENCRYPTION_KEY to the same value on every server process."
)


def _generate_secret() -> str:
    """Cryptographically secure 32-byte hex secret (256 bits)."""
    return secrets.token_hex(32)


async def resolve_session_secret(
    *, storage: StorageProvider, auth_config: AuthConfig,
) -> str:
    """Return the secret to use for cookie signing, persisting a
    freshly generated one if neither the env var nor the DB has a value."""
    if auth_config.session_secret:
        return auth_config.session_secret
    state = await storage.get_system_state()
    if state.session_secret:
        return state.session_secret
    new_secret = _generate_secret()
    await storage.set_session_secret(new_secret)
    return new_secret


async def resolve_secret_box(config: AppConfig, storage_provider: StorageProvider) -> SecretBox:
    """The box that seals the MCP OAuth tokens, client secrets and pending sign-ins this process stores.

    ``config.encryption_key`` is the key material when it is set; it must be at least :data:`MIN_ENCRYPTION_KEY_LENGTH` characters, else
    :class:`~primer.model.except_.ConfigError` (a weak key would look like protection and not be, so the process does not start). Without it the session
    secret is the material (:func:`resolve_session_secret`: the operator's, else the persisted one, generated on first need) and
    :data:`SECRET_BOX_FALLBACK_WARNING` is logged once. Every server process must resolve the same material, or the tokens one seals the others cannot open.
    """
    key = config.encryption_key
    if key is not None:
        material = key.get_secret_value()
        if len(material) < MIN_ENCRYPTION_KEY_LENGTH:
            raise ConfigError(f"PRIMER_ENCRYPTION_KEY must be at least {MIN_ENCRYPTION_KEY_LENGTH} characters")
        return SecretBox.from_key_material(material)
    logger.warning(SECRET_BOX_FALLBACK_WARNING)
    session_secret = await resolve_session_secret(storage=storage_provider, auth_config=config.auth)
    return SecretBox.from_key_material(session_secret)
