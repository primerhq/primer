"""Migration 1: retired.

Originally moved legacy document bodies into the content store. S2 made
the content store the single body location by clean break rather than by
migration, so there is nothing left to move. The class and its version
slot are retained so the chain still starts at 1 and existing installs
keep their ``SystemState.schema_version`` accounting.
"""

from __future__ import annotations

from primer.int.storage_provider import StorageProvider


class M001DocumentContent:
    """Retired no-op; the version slot is kept for chain numbering."""

    version = 1
    description = "retired: legacy document body migration removed in S2"

    async def apply(self, sp: StorageProvider) -> None:
        """Clean break (S2): legacy meta-body migration removed; slot
        retained for version numbering."""
        return None


__all__ = ["M001DocumentContent"]
