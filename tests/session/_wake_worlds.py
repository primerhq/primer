"""The worlds the one-turn-per-wake tests run in: the suite's in-memory storage AND a real SQLite storage (review N13 of the G7-03 fix).

The in-memory storage hands back the stored object itself, so a stale snapshot cannot exist there; SQLite reads a copy every time, as Postgres
does, so an interleaving that depends on a row read before another process wrote it is real there. Every cross-process test of
``test_one_turn_per_wake*.py`` and ``test_whole_row_writers_are_fenced.py`` takes ``new_world`` and runs once on each.

    async def test_x(new_world):
        world = await new_world(with_bus=True)
        install_sessions(world, wrapper)     # put a hooked session storage in front of the real one
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio

from primer.model.provider import SqliteConfig
from primer.model.workspace_session import WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from tests.session._peer_fakes import FakeBus, FakeEngine, FakeRegistry, FakeScheduler, World, make_world

STORAGES = ("memory", "sqlite")

NewWorld = Callable[..., Awaitable[World]]


@pytest_asyncio.fixture(params=STORAGES)
async def new_world(request: pytest.FixtureRequest, tmp_path: Any):
    """A factory of worlds on the storage this parametrization names. Every SQLite provider it opens is closed after the test."""
    opened: list[SqliteStorageProvider] = []

    async def factory(*, with_bus: bool = False) -> World:
        if request.param == "memory":
            return make_world(with_bus=with_bus)
        provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / f"world-{len(opened)}.sqlite")))
        await provider.initialize()
        await provider.get_event_store().ensure_schema()      # the app ensures it at boot; the delivery records its events there
        opened.append(provider)
        return World(
            sp=provider, registry=FakeRegistry(), scheduler=FakeScheduler(), engine=FakeEngine(), bus=FakeBus() if with_bus else None,
        )

    try:
        yield factory
    finally:
        for provider in opened:
            await provider.aclose()


def install_sessions(world: World, storage: Any) -> None:
    """Make ``storage`` the session storage every caller of ``world.sp.get_storage(WorkspaceSession)`` gets (a hooked wrapper in front of the
    real one): the in-memory provider keeps its storages in ``_stores``, the SQLite one its handles in ``_handles``."""
    table = getattr(world.sp, "_stores", None)
    if table is None:
        table = world.sp._handles
    table[WorkspaceSession] = storage


__all__ = ["STORAGES", "NewWorld", "install_sessions", "new_world"]
