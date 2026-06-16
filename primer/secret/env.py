"""Environment-variable-backed :class:`SecretProvider`.

Resolves a secret ``name`` to the env var ``<prefix><NAME>`` where
``<NAME>`` is the name upper-cased. With the default prefix
``PRIMER_SECRET_``, the secret ``deploy_key`` reads
``PRIMER_SECRET_DEPLOY_KEY``.
"""

from __future__ import annotations

import os

from pydantic import SecretStr

from primer.int.secret_provider import SecretProvider


class EnvSecretProvider(SecretProvider):
    """Read named secrets from process environment variables."""

    def __init__(self, prefix: str = "PRIMER_SECRET_") -> None:
        self._prefix = prefix

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get_secret(self, name: str) -> SecretStr | None:
        raw = os.environ.get(f"{self._prefix}{name.upper()}")
        if raw is None:
            return None
        return SecretStr(raw)
