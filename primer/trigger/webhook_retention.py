"""Bounded retention pruning for completed webhook deliveries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from primer.model.except_ import NotFoundError
from primer.model.storage import OffsetPage, OrderBy
from primer.model.webhook_delivery import WebhookDelivery
from primer.storage.q import Q


_PAGE = 100
_TERMINAL_STATUSES = ("done", "failed")


async def prune_webhook_deliveries(
    storage_provider: Any,
    *,
    retention_days: int,
    batch: int = _PAGE,
    now: datetime | None = None,
) -> int:
    """Delete up to ``batch`` terminal deliveries older than the cutoff.

    ``pending`` covers in-flight dispatch, interactive holds, and deliveries
    eligible for startup recovery, so it is never pruned regardless of age.
    Terminal rows without ``completed_at`` are also retained: the terminal
    timestamp, not acceptance time, is the retention clock.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    storage = storage_provider.get_storage(WebhookDelivery)
    predicate = (
        Q(WebhookDelivery)
        .where_in("status", _TERMINAL_STATUSES)
        .where_not_null("completed_at")
        .build()
    )
    deleted = 0
    while deleted < batch:
        page = await storage.find(
            predicate,
            OffsetPage(offset=0, length=_PAGE, want_total=False),
            order_by=[OrderBy(field="completed_at", direction="asc")],
        )
        if not page.items:
            break
        for row in page.items:
            # The backend predicate excludes NULL. The assertion documents
            # that invariant for type checkers without weakening a corrupt
            # backend into an unbounded Python-side skip loop.
            assert row.completed_at is not None
            completed_at = row.completed_at
            if completed_at >= cutoff:
                return deleted
            try:
                await storage.delete(row.id)
            except NotFoundError:
                pass
            else:
                deleted += 1
            if deleted >= batch:
                return deleted
        if len(page.items) < _PAGE:
            break
    return deleted


__all__ = ["prune_webhook_deliveries"]
