"""Migration 1: move legacy document bodies into the content store.

Wraps :func:`primer.knowledge.migration.migrate_document_content`, which
already ran ad-hoc from the API lifespan before the migration runner
existed. Because ``SystemState.schema_version`` defaults to ``1``, every
install that predates the runner is treated as having this migration
applied, which is accurate: the ad-hoc call was unconditional on every
boot. The wrapper exists so the chain starts at 1 and so a future
install that somehow sits below 1 still gets it; the underlying function
is idempotent either way.
"""

from __future__ import annotations

from primer.int.storage_provider import StorageProvider
from primer.knowledge.migration import migrate_document_content


class M001DocumentContent:
    """Backfill document paths and copy legacy bodies into the content store."""

    version = 1
    description = "migrate legacy document bodies into the content store"

    async def apply(self, sp: StorageProvider) -> None:
        await migrate_document_content(sp)


__all__ = ["M001DocumentContent"]
