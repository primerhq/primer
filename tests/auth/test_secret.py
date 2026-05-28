"""Tests for the session-secret resolver."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from primer.api.config import AuthConfig
from primer.auth.secret import resolve_session_secret
from primer.model.system_state import SystemState


class _FakeStorage:
    def __init__(self, initial_secret: str | None = None) -> None:
        self._secret = initial_secret
        self.set_calls: list[str] = []

    async def get_system_state(self) -> SystemState:
        return SystemState(session_secret=self._secret)

    async def set_session_secret(self, secret: str) -> None:
        self.set_calls.append(secret)
        self._secret = secret

    # Other ABC methods aren't called by the resolver; left unimplemented.
    async def set_bootstrap_completed(self, ts: datetime) -> None: ...
    async def initialize(self) -> None: ...
    async def aclose(self) -> None: ...
    def get_storage(self, model_class: type) -> Any: ...


@pytest.mark.asyncio
async def test_env_var_takes_precedence_over_db():
    storage = _FakeStorage(initial_secret="from-db")
    cfg = AuthConfig(session_secret="from-env")
    result = await resolve_session_secret(storage=storage, auth_config=cfg)
    assert result == "from-env"
    assert storage.set_calls == []  # nothing persisted


@pytest.mark.asyncio
async def test_db_value_used_when_env_unset():
    storage = _FakeStorage(initial_secret="from-db")
    cfg = AuthConfig(session_secret=None)
    result = await resolve_session_secret(storage=storage, auth_config=cfg)
    assert result == "from-db"
    assert storage.set_calls == []


@pytest.mark.asyncio
async def test_auto_generates_and_persists_when_neither_set():
    storage = _FakeStorage(initial_secret=None)
    cfg = AuthConfig(session_secret=None)
    result = await resolve_session_secret(storage=storage, auth_config=cfg)
    assert len(result) == 64  # 32-byte hex = 64 chars
    assert storage.set_calls == [result]


@pytest.mark.asyncio
async def test_idempotent_on_second_call_after_persist():
    storage = _FakeStorage(initial_secret=None)
    cfg = AuthConfig(session_secret=None)
    first = await resolve_session_secret(storage=storage, auth_config=cfg)
    second = await resolve_session_secret(storage=storage, auth_config=cfg)
    assert first == second
    assert len(storage.set_calls) == 1
