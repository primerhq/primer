"""hotpaths #4: list_triggers / list_subscriptions page through ALL rows.

Both previously capped at the first 200 rows. Seed 250 and assert all 250
are returned (filtered and unfiltered paths).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.model.provider import SqliteConfig
from primer.model.trigger import (
    AgentFreshSubConfig,
    DelayedTriggerConfig,
    Subscription,
    Trigger,
)
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger.service import (
    ServiceDeps,
    list_subscriptions,
    list_triggers,
)


def _trigger(i: int) -> Trigger:
    return Trigger(
        id=f"tr-{i}",
        slug=f"trig-{i}",
        name=f"Trigger {i}",
        config=DelayedTriggerConfig(fire_at=datetime.now(timezone.utc)),
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )


def _subscription(i: int, trigger_id: str) -> Subscription:
    return Subscription(
        id=f"sub-{i}",
        trigger_id=trigger_id,
        config=AgentFreshSubConfig(workspace_id="ws-x", agent_id="ag-x"),
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_list_triggers_returns_all_beyond_200(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    storage = provider.get_storage(Trigger)
    n = 250
    for i in range(n):
        await storage.create(_trigger(i))
    deps = ServiceDeps(storage_provider=provider)

    # Unfiltered path.
    items = await list_triggers(deps=deps)
    assert len(items) == n
    assert {t.id for t in items} == {f"tr-{i}" for i in range(n)}

    # Filtered path (enabled=True matches all 250).
    filtered = await list_triggers(enabled=True, deps=deps)
    assert len(filtered) == n
    await provider.aclose()


@pytest.mark.asyncio
async def test_list_subscriptions_returns_all_beyond_200(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    subs = provider.get_storage(Subscription)
    n = 250
    for i in range(n):
        await subs.create(_subscription(i, "tr-1"))
    deps = ServiceDeps(storage_provider=provider)

    items = await list_subscriptions(trigger_id="tr-1", deps=deps)
    assert len(items) == n
    assert {s.id for s in items} == {f"sub-{i}" for i in range(n)}
    await provider.aclose()
