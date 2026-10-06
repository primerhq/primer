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

from primer.model.except_ import NotFoundError
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


def _make_delete_fail_for(provider, bad_id: str, error: Exception) -> None:
    """Make the subscription store's ``delete`` raise ``error`` for ``bad_id`` and behave normally otherwise."""
    subs = provider.get_storage(Subscription)
    real_delete = subs.delete

    async def delete(id, *, conn=None):  # noqa: A002
        if id == bad_id:
            raise error
        await real_delete(id)

    subs.delete = delete  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_a_subscription_that_is_already_gone_does_not_stop_the_cascade(provider) -> None:
    """Deleting a subscription that a concurrent request already deleted is fine: the cascade carries on."""
    await _seed(provider, subscriptions=5)
    _make_delete_fail_for(provider, "sub-2", NotFoundError("Subscription with id 'sub-2' not found"))
    deps = ServiceDeps(storage_provider=provider)

    await delete_trigger(trigger_id="tr-1", deps=deps)

    assert await provider.get_storage(Trigger).get("tr-1") is None
    assert {s.id for s in await list_subscriptions(trigger_id="tr-1", deps=deps)} <= {"sub-2"}, "only the gone one remains"


@pytest.mark.asyncio
async def test_a_failure_to_delete_a_subscription_stops_the_cascade_and_keeps_the_trigger(provider) -> None:
    """A delete that FAILS (not "already gone") used to be swallowed and the trigger deleted over the subscription it
    could not delete: an orphan, silently. The error propagates and the trigger stays, so the delete can be retried."""
    await _seed(provider, subscriptions=5)
    _make_delete_fail_for(provider, "sub-2", RuntimeError("disk is full"))
    deps = ServiceDeps(storage_provider=provider)

    with pytest.raises(RuntimeError, match="disk is full"):
        await delete_trigger(trigger_id="tr-1", deps=deps)

    assert await provider.get_storage(Trigger).get("tr-1") is not None, "the trigger was deleted over an orphan"


@pytest.mark.asyncio
async def test_a_retry_after_a_failed_delete_finishes_the_cascade(provider) -> None:
    await _seed(provider, subscriptions=5)
    _make_delete_fail_for(provider, "sub-2", RuntimeError("disk is full"))
    deps = ServiceDeps(storage_provider=provider)
    with pytest.raises(RuntimeError):
        await delete_trigger(trigger_id="tr-1", deps=deps)
    del provider.get_storage(Subscription).delete          # the fault clears: the instance attribute goes, the method is back

    await delete_trigger(trigger_id="tr-1", deps=deps)

    assert await list_subscriptions(trigger_id="tr-1", deps=deps) == []
    assert await provider.get_storage(Trigger).get("tr-1") is None


@pytest.mark.asyncio
async def test_another_triggers_subscriptions_are_left_alone(provider) -> None:
    await _seed(provider, subscriptions=450, neighbour_subscriptions=3)
    deps = ServiceDeps(storage_provider=provider)

    await delete_trigger(trigger_id="tr-1", deps=deps)

    assert {s.id for s in await list_subscriptions(trigger_id="tr-2", deps=deps)} == {"other-0", "other-1", "other-2"}
    assert await provider.get_storage(Trigger).get("tr-2") is not None
