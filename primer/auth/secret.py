"""Resolve the session-signing secret.

Priority:
1. ``AuthConfig.session_secret`` (loaded from ``PRIMER_SESSION_SECRET``
   env var). Operator-set; takes precedence.
2. ``system_state.session_secret`` column. Auto-generated on first
   need, persisted for restart durability.

If neither is set, this module generates a fresh 32-byte hex secret
and writes it back via :meth:`StorageProvider.set_session_secret`.
"""

from __future__ import annotations

import secrets

from primer.api.config import AuthConfig
from primer.int.storage_provider import StorageProvider


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
