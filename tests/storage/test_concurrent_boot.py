"""Processes that boot together on one database (GR-1).

``primer api --no-worker`` and ``primer worker`` both run the migration chain at boot, and Kubernetes rolls their pods at once. The chain runs
under one cross-process lock (a Postgres advisory lock, a file lock beside a SQLite database), the runner re-reads ``schema_version`` inside it,
and the stamp never moves the version back. The SQLite cases run everywhere; the Postgres cases are gated by ``tests/pg_gate.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

import primer.storage.migrations as mig
from primer.int.storage_provider import StorageProvider
from primer.model.provider import SqliteConfig
from primer.model.providers.llm import LLMProvider
from primer.model.user import User
from primer.storage.migrations.m028_infrastructure_public import M028InfrastructurePublic
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import postgres_marks, require_postgres_url
from tests.storage.test_system_state import _parse_postgres_url

_RESET = "SET bootstrap_completed_at = NULL, last_migration_at = NULL, schema_version = 1, session_secret = NULL"


async def _reset_pg(provider: PostgresStorageProvider) -> None:
    async with provider.pool.acquire() as conn:
        await conn.execute(f'UPDATE "{provider.schema}"."system_state" {_RESET}')


@pytest_asyncio.fixture(params=["sqlite", pytest.param("postgres", marks=postgres_marks("the concurrent-boot tests"))])
async def two_boots(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[tuple[StorageProvider, StorageProvider]]:
    """Two providers on ONE database, as two processes would hold them."""
    if request.param == "sqlite":
        providers = [SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite")) for _ in range(2)]
    else:
        cfg = _parse_postgres_url(require_postgres_url("the concurrent-boot tests"))
        providers = [PostgresStorageProvider(cfg) for _ in range(2)]
    for p in providers:
        await p.initialize()
    if request.param == "postgres":
        await _reset_pg(providers[0])
    try:
        yield providers[0], providers[1]
    finally:
        if request.param == "postgres":
            await _reset_pg(providers[0])
        for p in providers:
            await p.aclose()


class _Gated:
    """A synthetic migration whose first run waits on ``gate`` (a slow table, a GC pause); every run is recorded with the provider it ran on."""

    def __init__(self, version: int, *, gate: asyncio.Event, entered: asyncio.Event, log: list[tuple[int, int]], fail_once: bool = False) -> None:
        self.version, self.description = version, f"gated {version}"
        self._gate, self._entered, self._log, self._fail_once = gate, entered, log, fail_once
        self._calls = 0

    async def apply(self, sp: StorageProvider) -> None:
        self._calls += 1
        if self._calls == 1:
            self._entered.set()
            await self._gate.wait()
            if self._fail_once:
                raise RuntimeError(f"migration {self.version} failed")
        self._log.append((id(sp), self.version))


def _record_stamps(monkeypatch: pytest.MonkeyPatch, *providers: StorageProvider) -> list[int]:
    stamps: list[int] = []
    for sp in providers:
        real = sp.set_schema_version

        async def _stamp(version, *, migrated_at=None, _real=real):
            stamps.append(version)
            await _real(version, migrated_at=migrated_at)

        monkeypatch.setattr(sp, "set_schema_version", _stamp)
    return stamps


async def test_a_second_boot_waits_for_the_first_then_applies_nothing(two_boots, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = two_boots
    gate, entered, log = asyncio.Event(), asyncio.Event(), []
    monkeypatch.setattr(mig, "MIGRATIONS", (_Gated(2, gate=gate, entered=entered, log=log), _Gated(3, gate=asyncio.Event(), entered=asyncio.Event(), log=log)))
    monkeypatch.setattr(mig, "LATEST_VERSION", 3)
    mig.MIGRATIONS[1]._gate.set()
    stamps = _record_stamps(monkeypatch, a, b)

    first = asyncio.create_task(mig.run_migrations(a, is_fresh_install=False))
    await entered.wait()
    second = asyncio.create_task(mig.run_migrations(b, is_fresh_install=False))
    await asyncio.wait({second}, timeout=0.5)  # room for an unlocked second boot to run the chain
    gate.set()

    assert await asyncio.wait_for(first, 10) == 3
    assert await asyncio.wait_for(second, 10) == 3
    assert log == [(id(a), 2), (id(a), 3)], "only the first boot applied the chain"
    assert stamps == [2, 3], "the version never stepped back"
    assert (await b.get_system_state()).schema_version == 3


@pytest.mark.parametrize("exit_via", ["error", "cancel"])
async def test_the_lock_is_released_on_every_exit_path(two_boots, monkeypatch: pytest.MonkeyPatch, exit_via: str) -> None:
    a, b = two_boots
    gate, entered, log = asyncio.Event(), asyncio.Event(), []
    monkeypatch.setattr(mig, "MIGRATIONS", (_Gated(2, gate=gate, entered=entered, log=log, fail_once=exit_via == "error"),))
    monkeypatch.setattr(mig, "LATEST_VERSION", 2)

    first = asyncio.create_task(mig.run_migrations(a, is_fresh_install=False))
    await entered.wait()
    if exit_via == "error":
        gate.set()
        with pytest.raises(RuntimeError, match="migration 2 failed"):
            await first
    else:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert (await b.get_system_state()).schema_version == 1
    assert await asyncio.wait_for(mig.run_migrations(b, is_fresh_install=False), 10) == 2
    assert log == [(id(b), 2)]


async def test_a_lower_stamp_never_moves_the_version_back(two_boots) -> None:
    a, _ = two_boots
    await a.set_schema_version(29)
    await a.set_schema_version(28)
    assert (await a.get_system_state()).schema_version == 29


async def test_a_lagging_boot_does_not_rerun_m028_on_a_new_private_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The GR-1 probe inverted: boot B reaches m028 first and is slow there; boot A, started meanwhile, waits instead of overtaking it."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await sp.initialize()
    try:
        await sp.get_storage(User).create(User(id="admin-1", username="admin", password_hash="x", created_at=datetime(2026, 1, 1, tzinfo=UTC), role="admin"))
        await sp.set_schema_version(27)
        gate, entered, calls = asyncio.Event(), asyncio.Event(), []
        real_apply = M028InfrastructurePublic.apply

        async def lagging_first(self, sp_):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                await gate.wait()
            await real_apply(self, sp_)

        monkeypatch.setattr(M028InfrastructurePublic, "apply", lagging_first)
        stamps = _record_stamps(monkeypatch, sp)

        boot_b = asyncio.create_task(mig.run_migrations(sp, is_fresh_install=False))
        await entered.wait()
        boot_a = asyncio.create_task(mig.run_migrations(sp, is_fresh_install=False))
        await asyncio.wait({boot_a}, timeout=0.5)
        gate.set()
        assert await asyncio.wait_for(boot_b, 10) == 29

        # B serves; an admin creates a provider, private by default.
        await sp.get_storage(LLMProvider).create(
            LLMProvider.model_validate({"id": "prov-private", "provider": "anthropic", "config": {"api_key": "k"}, "limits": {"max_concurrency": 1}})
        )
        assert await asyncio.wait_for(boot_a, 10) == 29

        after = await sp.get_storage(LLMProvider).get("prov-private")
        assert not after.access.public and after.access.members == []
        assert len(calls) == 1, "m028 ran exactly once"
        assert stamps == [28, 29]
    finally:
        await sp.aclose()


async def test_concurrent_first_boots_agree_on_one_session_secret(two_boots) -> None:
    """GS-14: a secret is stored only when none is, and every resolver returns the stored one (cookies and the token box agree)."""
    from primer.api.config import AuthConfig
    from primer.auth.secret import resolve_session_secret

    a, b = two_boots
    cfg = AuthConfig(session_secret=None)
    got = await asyncio.gather(*(resolve_session_secret(storage=p, auth_config=cfg) for p in (a, b, a, b)))
    stored = (await b.get_system_state()).session_secret
    assert stored and set(got) == {stored}
    await a.set_session_secret("late")
    assert (await b.get_system_state()).session_secret == stored
