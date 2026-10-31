"""The lifespan decides "fresh install" from the data, never from the bootstrap marker alone (core review finding 5).

An install whose ``bootstrap_completed_at`` is empty (``auto_bootstrap=False``, or a first boot whose bootstrap had errors) used to count as
fresh on EVERY boot, so an upgrade stamped the latest schema version without running its pending data migrations. An install that was ever
booted (it has a migration stamp) or holds rows is an existing install and always runs what is pending; only an empty one baselines.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import primer.storage.migrations as mig
from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.model.scheduler import RuntimeMode
from primer.model.user import User
from tests.api.conftest import _FakeStorageProvider


def _app(monkeypatch, storage, *, auto_bootstrap=False):
    monkeypatch.setattr("primer.api.app._build_storage_provider", lambda _cfg: storage)
    return create_app(AppConfig(runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None, auto_bootstrap=auto_bootstrap))


def _record_applies(monkeypatch, above: int) -> list[int]:
    applied: list[int] = []
    for m in mig.MIGRATIONS:
        if m.version > above:
            async def _apply(_sp, v=m.version):
                applied.append(v)
            monkeypatch.setattr(m, "apply", _apply)
    return applied


async def _boot(app):
    async with app.router.lifespan_context(app):
        pass


async def _existing_install(storage, *, version: int) -> None:
    """An install an earlier build booted: it stamped ``version`` (so a migration stamp exists) and has a user; the bootstrap marker stayed empty."""
    await storage.set_schema_version(version, migrated_at=datetime(2026, 1, 1, tzinfo=UTC))
    await storage.get_storage(User).create(User(id="u1", username="ada", created_at=datetime(2026, 1, 1, tzinfo=UTC), role="admin"))


@pytest.mark.asyncio
async def test_an_install_without_a_bootstrap_marker_runs_its_pending_migrations(monkeypatch):
    storage = _FakeStorageProvider()
    await _existing_install(storage, version=20)
    applied = _record_applies(monkeypatch, 20)
    await _boot(_app(monkeypatch, storage))
    assert applied == [m.version for m in mig.MIGRATIONS if m.version > 20]
    assert (await storage.get_system_state()).schema_version == mig.LATEST_VERSION


@pytest.mark.asyncio
async def test_an_install_at_the_latest_version_with_no_marker_is_not_restamped(monkeypatch):
    """A first bootstrap that had errors leaves the marker empty; the next boot must not treat the install as new."""
    storage = _FakeStorageProvider()
    await _existing_install(storage, version=mig.LATEST_VERSION)
    stamped_at = (await storage.get_system_state()).last_migration_at
    applied = _record_applies(monkeypatch, 0)
    await _boot(_app(monkeypatch, storage))
    state = await storage.get_system_state()
    assert applied == []
    assert state.schema_version == mig.LATEST_VERSION and state.last_migration_at == stamped_at


@pytest.mark.asyncio
async def test_an_install_with_rows_but_no_stamp_is_an_existing_install(monkeypatch):
    """Rows and no migration stamp at all (an install from before migrations were stamped, bootstrap off): version 1 runs the whole chain."""
    storage = _FakeStorageProvider()
    await storage.get_storage(User).create(User(id="u1", username="ada", created_at=datetime(2026, 1, 1, tzinfo=UTC), role="admin"))
    applied = _record_applies(monkeypatch, 0)
    await _boot(_app(monkeypatch, storage))
    assert applied and applied == sorted(applied) and applied[-1] == mig.LATEST_VERSION


@pytest.mark.asyncio
async def test_a_truly_empty_install_baselines_without_running_migrations(monkeypatch):
    """Characterization (passes before and after): a new install has no rows to transform and is stamped straight to the latest version."""
    storage = _FakeStorageProvider()
    applied = _record_applies(monkeypatch, 0)
    await _boot(_app(monkeypatch, storage))
    state = await storage.get_system_state()
    assert applied == [] and state.schema_version == mig.LATEST_VERSION and state.last_migration_at is not None


@pytest.mark.asyncio
async def test_a_normal_upgrade_with_a_bootstrap_marker_runs_pending_migrations(monkeypatch):
    """Characterization (passes before and after)."""
    storage = _FakeStorageProvider()
    await _existing_install(storage, version=20)
    await storage.set_bootstrap_completed(datetime(2026, 1, 1, tzinfo=UTC))
    applied = _record_applies(monkeypatch, 20)
    await _boot(_app(monkeypatch, storage))
    assert applied == [m.version for m in mig.MIGRATIONS if m.version > 20]
