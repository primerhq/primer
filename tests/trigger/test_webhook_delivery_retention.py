"""Webhook delivery retention keeps recoverable rows and prunes old terminals."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from primer.api.config import AppConfig
from primer.bus.scheduler_tasks import WebhookDeliveryRetentionPruner
from primer.coordinator.in_memory import InMemoryLeaderElector
from primer.int.coordinator import ROLE_WEBHOOK_DELIVERY_RETENTION
from primer.model.common import dump_for_storage
from primer.model.except_ import NotFoundError
from primer.model.storage import FieldRef, Op, Predicate
from primer.model.webhook_delivery import WebhookDelivery
from primer.trigger.webhook_retention import prune_webhook_deliveries


NOW = datetime(2026, 11, 1, tzinfo=UTC)


async def _delivery(
    storage_provider,
    delivery_id: str,
    *,
    status: str,
    completed_days_ago: float | None,
    created_days_ago: float = 60,
    attempts: int = 1,
    fire_id: str | None = None,
) -> None:
    await storage_provider.get_storage(WebhookDelivery).create(
        WebhookDelivery(
            id=delivery_id,
            trigger_id="trigger-1",
            status=status,
            created_at=NOW - timedelta(days=created_days_ago),
            completed_at=(
                None
                if completed_days_ago is None
                else NOW - timedelta(days=completed_days_ago)
            ),
            attempts=attempts,
            fire_id=fire_id,
        )
    )


async def _ids(storage_provider) -> set[str]:
    from primer.model.storage import OffsetPage

    page = await storage_provider.get_storage(WebhookDelivery).list(
        OffsetPage(offset=0, length=200)
    )
    return {row.id for row in page.items}


def test_config_defaults_to_thirty_days_and_rejects_non_positive_values():
    assert AppConfig.model_fields["webhook_delivery_retention_days"].default == 30
    assert AppConfig(webhook_delivery_retention_days=14).webhook_delivery_retention_days == 14
    with pytest.raises(ValueError):
        AppConfig(webhook_delivery_retention_days=0)


def test_timestamps_normalize_to_fixed_width_utc_storage_text():
    delivery = WebhookDelivery(
        id="offset-and-naive",
        trigger_id="trigger-1",
        status="done",
        created_at=datetime(
            2026,
            11,
            1,
            5,
            30,
            tzinfo=timezone(timedelta(hours=5, minutes=30)),
        ),
        completed_at=datetime(2026, 11, 1, 0, 0, 0, 7),
    )

    assert delivery.created_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert delivery.completed_at == datetime(2026, 11, 1, 0, 0, 0, 7, tzinfo=UTC)
    stored = dump_for_storage(delivery)
    assert stored["created_at"] == "2026-11-01T00:00:00.000000Z"
    assert stored["completed_at"] == "2026-11-01T00:00:00.000007Z"


async def test_prunes_only_terminal_rows_strictly_older_than_cutoff(
    fake_storage_provider,
):
    await _delivery(fake_storage_provider, "old-done", status="done", completed_days_ago=31)
    await _delivery(fake_storage_provider, "old-failed", status="failed", completed_days_ago=40)
    await _delivery(fake_storage_provider, "boundary", status="done", completed_days_ago=30)
    await _delivery(fake_storage_provider, "recent", status="failed", completed_days_ago=3)
    await _delivery(fake_storage_provider, "terminal-no-time", status="done", completed_days_ago=None)
    await _delivery(fake_storage_provider, "pending-stale", status="pending", completed_days_ago=None)
    await _delivery(
        fake_storage_provider,
        "pending-retry",
        status="pending",
        completed_days_ago=None,
        attempts=2,
    )
    await _delivery(
        fake_storage_provider,
        "pending-hold",
        status="pending",
        completed_days_ago=None,
        fire_id="fire-live",
    )

    assert await prune_webhook_deliveries(
        fake_storage_provider, retention_days=30, batch=100, now=NOW
    ) == 2
    assert await _ids(fake_storage_provider) == {
        "boundary",
        "recent",
        "terminal-no-time",
        "pending-stale",
        "pending-retry",
        "pending-hold",
    }


async def test_batching_is_bounded_and_repeated_sweeps_converge(
    fake_storage_provider,
):
    for i in range(5):
        await _delivery(
            fake_storage_provider,
            f"old-{i}",
            status="done" if i % 2 else "failed",
            completed_days_ago=40 + i,
        )

    assert await prune_webhook_deliveries(
        fake_storage_provider, retention_days=30, batch=2, now=NOW
    ) == 2
    assert await prune_webhook_deliveries(
        fake_storage_provider, retention_days=30, batch=2, now=NOW
    ) == 2
    assert await prune_webhook_deliveries(
        fake_storage_provider, retention_days=30, batch=2, now=NOW
    ) == 1
    assert await prune_webhook_deliveries(
        fake_storage_provider, retention_days=30, batch=2, now=NOW
    ) == 0
    assert await _ids(fake_storage_provider) == set()


def _predicate_has(predicate: Predicate, field: str, op: Op) -> bool:
    if predicate.op == op and isinstance(predicate.left, FieldRef):
        return predicate.left.name == field
    return any(
        _predicate_has(child, field, op)
        for child in (predicate.left, predicate.right)
        if isinstance(child, Predicate)
    )


async def test_null_terminal_rows_are_filtered_before_bounded_paging(
    fake_storage_provider,
    monkeypatch,
):
    for i in range(150):
        await _delivery(
            fake_storage_provider,
            f"terminal-null-{i:03d}",
            status="done",
            completed_days_ago=None,
        )
    await _delivery(
        fake_storage_provider,
        "old-terminal",
        status="failed",
        completed_days_ago=40,
    )

    storage = fake_storage_provider.get_storage(WebhookDelivery)
    real_find = storage.find
    calls = 0

    async def recording_find(predicate, page, *, order_by=None):
        nonlocal calls
        calls += 1
        assert _predicate_has(predicate, "completed_at", Op.IS_NOT_NULL)
        result = await real_find(predicate, page, order_by=order_by)
        assert all(row.completed_at is not None for row in result.items)
        return result

    monkeypatch.setattr(storage, "find", recording_find)

    assert await prune_webhook_deliveries(
        fake_storage_provider, retention_days=30, batch=1, now=NOW
    ) == 1
    assert calls == 1
    assert len(await _ids(fake_storage_provider)) == 150


async def test_not_found_race_does_not_consume_the_exact_batch(
    fake_storage_provider,
    monkeypatch,
):
    for i in range(5):
        await _delivery(
            fake_storage_provider,
            f"old-{i}",
            status="done",
            completed_days_ago=40 + i,
        )

    storage = fake_storage_provider.get_storage(WebhookDelivery)
    real_delete = storage.delete
    raced = False
    calls = 0

    async def racing_delete(delivery_id: str):
        nonlocal raced, calls
        calls += 1
        if not raced:
            raced = True
            await real_delete(delivery_id)
            raise NotFoundError("deleted by a concurrent pruner")
        await real_delete(delivery_id)

    monkeypatch.setattr(storage, "delete", racing_delete)

    assert await prune_webhook_deliveries(
        fake_storage_provider, retention_days=30, batch=3, now=NOW
    ) == 3
    assert calls == 4
    assert len(await _ids(fake_storage_provider)) == 1


async def test_pruner_uses_leader_task_lifecycle(fake_storage_provider, monkeypatch):
    pruner = WebhookDeliveryRetentionPruner(
        storage_provider=fake_storage_provider,
        retention_days=30,
        interval_seconds=3600,
    )
    assert pruner.role == ROLE_WEBHOOK_DELIVERY_RETENTION

    ticked = asyncio.Event()

    async def sweep_once(*, now=None):
        ticked.set()
        return 0

    monkeypatch.setattr(pruner, "sweep_once", sweep_once)
    pruner.start(InMemoryLeaderElector())
    await asyncio.wait_for(ticked.wait(), timeout=1)
    await pruner.stop()
    assert pruner._task is None
