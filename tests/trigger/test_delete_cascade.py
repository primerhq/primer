"""``delete_trigger`` cascades to EVERY subscription of the trigger, however many there are (task 01a111d1, D2).

The cascade used to page the subscriptions with ``OffsetPage(offset, 200)``, delete each row of the page, and then advance the
offset by 200. The rows deleted from a page shift the remaining ones down, so the next page started 200 rows too far on: with
more than 200 subscriptions the cascade skipped 200 rows after the first page, and the trigger row was deleted anyway,
leaving orphan subscriptions pointing at a trigger that no longer exists. Nothing was cancelled and nothing raised.
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
from primer.trigger.service import ServiceDeps, delete_trigger, list_subscriptions


def _trigger(trigger_id: str) -> Trigger:
    return Trigger(
        id=trigger_id,
        slug=f"slug-{trigger_id}",
        name=f"Trigger {trigger_id}",
        config=DelayedTriggerConfig(fire_at=datetime.now(timezone.utc)),
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )


def _subscription(sub_id: str, trigger_id: str) -> Subscription:
    return Subscription(
        id=sub_id,
        trigger_id=trigger_id,
        config=AgentFreshSubConfig(workspace_id="ws-x", agent_id="ag-x"),
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
async def provider(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


async def _seed(provider, *, subscriptions: int, neighbour_subscriptions: int = 3) -> None:
    triggers = provider.get_storage(Trigger)
    subs = provider.get_storage(Subscription)
    await triggers.create(_trigger("tr-1"))
    await triggers.create(_trigger("tr-2"))
    for i in range(subscriptions):
        await subs.create(_subscription(f"sub-{i}", "tr-1"))
    for i in range(neighbour_subscriptions):
        await subs.create(_subscription(f"other-{i}", "tr-2"))


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 199, 200, 201, 400, 450])
async def test_every_subscription_of_the_trigger_is_deleted_whatever_their_number(provider, count: int) -> None:
    await _seed(provider, subscriptions=count)
    deps = ServiceDeps(storage_provider=provider)

    await delete_trigger(trigger_id="tr-1", deps=deps)

    assert await list_subscriptions(trigger_id="tr-1", deps=deps) == [], "orphan subscriptions of a deleted trigger"
    assert await provider.get_storage(Trigger).get("tr-1") is None


@pytest.mark.asyncio
async def test_another_triggers_subscriptions_are_left_alone(provider) -> None:
    await _seed(provider, subscriptions=450, neighbour_subscriptions=3)
    deps = ServiceDeps(storage_provider=provider)

    await delete_trigger(trigger_id="tr-1", deps=deps)

    assert {s.id for s in await list_subscriptions(trigger_id="tr-2", deps=deps)} == {"other-0", "other-1", "other-2"}
    assert await provider.get_storage(Trigger).get("tr-2") is not None
