"""Per-(server, principal, toolset) OAuth token cache.

The cache is keyed by an opaque string the
:class:`matrix.toolset.oauth.handler.MatrixOAuthHandler` constructs
from the auth server's origin, the caller-supplied principal, and the
toolset id. Entries are evicted on read once their ``expires_at`` has
passed; nothing in this module emits a periodic sweep -- short-lived
processes don't need one, and a long-running consumer can compose a
janitor task externally if desired.

The :class:`TokenStore` Protocol allows alternative back-ends (Redis,
disk) to be swapped in without changing the OAuth subsystem.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel, Field, SecretStr


class TokenRecord(BaseModel):
    """A single cached OAuth grant."""

    access_token: SecretStr = Field(
        ...,
        description="The bearer token to send on the Authorization header.",
    )
    refresh_token: SecretStr | None = Field(
        default=None,
        description="OAuth refresh token, if the auth server issued one.",
    )
    expires_at: datetime = Field(
        ...,
        description="UTC instant after which the access_token is no longer valid.",
    )
    token_type: str = Field(
        default="Bearer",
        description="OAuth token_type from the token response (almost always 'Bearer').",
    )


class TokenStore(Protocol):
    async def get(self, key: str) -> TokenRecord | None: ...
    async def set(self, key: str, record: TokenRecord) -> None: ...
    async def delete(self, key: str) -> None: ...


class InMemoryTokenStore:
    """Process-local :class:`TokenStore`. Eviction on get when expired."""

    def __init__(self) -> None:
        self._store: dict[str, TokenRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> TokenRecord | None:
        async with self._lock:
            rec = self._store.get(key)
            if rec is None:
                return None
            if rec.expires_at <= datetime.now(timezone.utc):
                del self._store[key]
                return None
            return rec

    async def set(self, key: str, record: TokenRecord) -> None:
        async with self._lock:
            self._store[key] = record

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)
