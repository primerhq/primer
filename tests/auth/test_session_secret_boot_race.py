"""GS-14: processes booting a fresh install together end with ONE session secret (real SQLite).

The secret signs cookies and, without ``PRIMER_ENCRYPTION_KEY``, keys the box that seals MCP OAuth tokens, so every process must use the stored
value. The store writes a secret only when none is stored, and the resolver returns what the database holds after its write.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from primer.api.config import AuthConfig
from primer.auth.secret import resolve_session_secret
from primer.model.providers.storage import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def test_two_concurrent_first_boots_end_with_the_stored_secret(sp) -> None:
    cfg = AuthConfig(session_secret=None)
    a, b = await asyncio.gather(resolve_session_secret(storage=sp, auth_config=cfg), resolve_session_secret(storage=sp, auth_config=cfg))
    stored = (await sp.get_system_state()).session_secret
    assert a == b == stored
    assert await resolve_session_secret(storage=sp, auth_config=cfg) == stored, "a later boot reads the same secret"


async def test_a_second_store_never_replaces_the_first(sp) -> None:
    await sp.set_session_secret("first")
    await sp.set_session_secret("second")
    assert (await sp.get_system_state()).session_secret == "first"


async def test_a_single_boot_keeps_its_order_config_then_stored_then_generated(sp) -> None:
    """Characterization: the operator's secret wins and writes nothing; else one is generated, stored, and read back on the next boot."""
    assert await resolve_session_secret(storage=sp, auth_config=AuthConfig(session_secret="operator")) == "operator"
    assert (await sp.get_system_state()).session_secret is None
    generated = await resolve_session_secret(storage=sp, auth_config=AuthConfig(session_secret=None))
    assert len(generated) == 64 and (await sp.get_system_state()).session_secret == generated
    assert await resolve_session_secret(storage=sp, auth_config=AuthConfig(session_secret=None)) == generated
