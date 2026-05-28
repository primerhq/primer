"""Single-use OAuth state payloads, keyed by UUID.

The :class:`PrimerOAuthHandler` issues one :class:`OAuthState` payload
per authorization-URL build, persists it via :class:`StateStore`, and
embeds the resulting UUID as the ``state`` query parameter on the URL.
When the user returns from consent, the application's callback handler
delivers ``code`` + ``state`` back through
:meth:`McpToolsetProvider.complete_oauth`. The handler calls
:meth:`StateStore.take`, which returns the original payload AND
deletes it -- state is single-use; replaying the same state is a
:class:`BadRequestError`.

The default in-memory implementation evicts expired entries on
:meth:`take`; nothing in this module sweeps the dict periodically.
Configure a longer TTL (10 minutes is the spec default) to keep
forgotten flows from sitting around indefinitely.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, Field, HttpUrl, SecretStr

from primer.model.except_ import BadRequestError


SpecVersion = Literal["2025-03-26", "2025-06-18", "2025-11-25"]


class OAuthState(BaseModel):
    """The payload persisted between authorization-URL build and token exchange."""

    principal: str | None = Field(
        ...,
        description="Caller-supplied end-user identity, or None for anonymous.",
    )
    toolset_id: str = Field(
        ...,
        min_length=1,
        description="Toolset id this flow belongs to.",
    )
    code_verifier: SecretStr = Field(
        ...,
        description="PKCE code_verifier; matched against the code_challenge sent on the authorization URL.",
    )
    spec_version: SpecVersion = Field(
        ...,
        description="Which MCP authorization spec version this flow follows.",
    )
    auth_server_metadata_url: HttpUrl = Field(
        ...,
        description="URL the discovery step fetched -- re-used to look up token_endpoint when completing.",
    )
    issued_at: datetime = Field(
        ...,
        description="UTC instant the state was created.",
    )


class StateStore(Protocol):
    async def put(self, payload: OAuthState, ttl: timedelta) -> str: ...
    async def take(self, state_id: str) -> OAuthState: ...


class _Entry(BaseModel):
    payload: OAuthState
    expires_at: datetime


class InMemoryStateStore:
    """UUID-keyed in-memory :class:`StateStore`. Single-use, TTL-evicted."""

    def __init__(self) -> None:
        self._store: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def put(self, payload: OAuthState, ttl: timedelta) -> str:
        sid = uuid.uuid4().hex
        async with self._lock:
            self._store[sid] = _Entry(
                payload=payload,
                expires_at=datetime.now(timezone.utc) + ttl,
            )
        return sid

    async def take(self, state_id: str) -> OAuthState:
        async with self._lock:
            entry = self._store.pop(state_id, None)
            if entry is None:
                raise BadRequestError(
                    f"OAuth state {state_id!r} not found, expired, or already consumed"
                )
            if entry.expires_at <= datetime.now(timezone.utc):
                raise BadRequestError(
                    f"OAuth state {state_id!r} expired"
                )
            return entry.payload
