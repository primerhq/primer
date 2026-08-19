"""Audit-row bookkeeping for external tool calls.

Ported from ``primer/chat/pending.py`` when S6 P5 deleted the carved-out
chat engine. The helper was never chat-specific: it takes any ``Storage``
handle and an audit row id, which is why it moved rather than died.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


async def flip_external_row(
    storage: Any,
    *,
    row_id: str | None,
    status: str,
    result: Any,
    is_error: bool = True,
) -> None:
    """Best-effort resolve of an external call's audit row.

    The park/pending slot is the execution source of truth; a missing or
    already-resolved row must never fail the surrounding flow, so every
    error is swallowed after the guard checks.
    """
    if not row_id:
        return
    try:
        row = await storage.get(row_id)
        if row is None or row.status != "pending":
            return
        row.status = status
        row.result = result
        row.is_error = is_error
        row.resolved_at = datetime.now(timezone.utc)
        await storage.update(row)
    except Exception:  # noqa: BLE001 - audit row is best-effort
        import logging

        logging.getLogger(__name__).exception(
            "external tool call row flip failed for %r", row_id
        )


__all__ = ["flip_external_row"]
