"""Migration 31: canonicalize webhook delivery timestamps for retention order.

Webhook delivery retention reads ``completed_at ASC, id ASC`` through the
generic JSON storage layer. Both backends therefore order the stored timestamp
text. Earlier models accepted offsets and naive values and used Pydantic's
variable-width datetime spelling, none of which guarantees chronological text
order. Rewrite every existing row through a migration-local model that keeps
unknown fields and uses the live fixed-width UTC timestamp type.
"""

from __future__ import annotations

import logging

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.webhook_delivery import WebhookTimestamp
from primer.storage.migrations._rows import iter_rows

logger = logging.getLogger(__name__)


class WebhookDelivery(Identifiable):  # noqa: N801 - selects the existing table
    """Migration-local view that preserves every non-timestamp field."""

    model_config = ConfigDict(extra="allow")

    created_at: WebhookTimestamp
    completed_at: WebhookTimestamp | None = None


class M031WebhookDeliveryTimestamps:
    """Rewrite historical timestamp spellings to fixed-width UTC."""

    version = 31
    description = "canonicalize webhook delivery timestamps as fixed-width UTC"

    async def apply(self, sp: StorageProvider) -> None:
        storage = sp.get_storage(WebhookDelivery)
        rewritten = 0
        async for row in iter_rows(storage):
            # A whole-row update is intentional: this migration-local view
            # retains unknown fields and its storage serializer rewrites both
            # timestamps in one idempotent operation.
            await storage.update(row)
            rewritten += 1
        if rewritten:
            logger.info(
                "canonicalized webhook delivery timestamps",
                extra={"rows": rewritten},
            )


__all__ = ["M031WebhookDeliveryTimestamps"]
