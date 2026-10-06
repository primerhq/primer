"""Audit-row bookkeeping for external tool calls.

Ported from ``primer/chat/pending.py`` when S6 P5 deleted the carved-out
chat engine. The helper was never chat-specific: it takes any ``Storage``
handle and an audit row id, which is why it moved rather than died.

Every write of an ``ExternalToolCall`` status goes through
:func:`resolve_external_row`: the result path (``completed``), the
cancels (``cancelled``) and the read surface's lazy timeout
(``timed_out``). It is one guarded write, so the first terminal status
a row reaches is the one it keeps.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic_core import to_jsonable_python

from primer.model.except_ import NotFoundError
from primer.model.external_tool import ExternalToolCall, ExternalToolCallStatus

logger = logging.getLogger(__name__)


async def resolve_external_row(
    storage: Any,
    row_id: str,
    *,
    status: ExternalToolCallStatus,
    result: Any,
    is_error: bool,
) -> ExternalToolCall | None:
    """Move one ``pending`` row to ``status`` with ONE guarded write.

    ``patch_if(row_id, {status, result, is_error, resolved_at},
    where={"status": ["pending"]})``: the database applies it only while
    the row is still ``pending``, so two writers racing on one call
    cannot overwrite each other (a whole-row ``update`` of a snapshot
    read earlier could write ``timed_out`` or ``cancelled`` over a
    ``completed`` that landed in between). It writes only the four
    fields it owns.

    Returns the updated row, or ``None`` when the guard rejected the
    write: the row had already left ``pending``, its terminal status was
    written by whoever moved it, and it stands. Raises on a storage
    error, including ``NotFoundError`` for a row that does not exist.

    The patch is encoded to JSON first (``to_jsonable_python``) the way a
    whole-row write dumps the model: NaN and the infinities become
    ``null`` (``inf_nan_mode="null"``, as ``dump_for_storage`` does), so
    a result carrying one is stored exactly as the park receives it. A
    ``result`` no JSON can hold (an arbitrary object, bytes that are not
    UTF-8, a lone surrogate) raises here and writes nothing.
    """
    patch = to_jsonable_python(
        {
            "status": status,
            "result": result,
            "is_error": is_error,
            "resolved_at": datetime.now(UTC),
        },
        inf_nan_mode="null",
    )
    return await storage.patch_if(row_id, patch, where={"status": ["pending"]})


async def flip_external_row(
    storage: Any,
    *,
    row_id: str | None,
    status: ExternalToolCallStatus,
    result: Any,
    is_error: bool = True,
) -> None:
    """Best-effort resolve of an external call's audit row.

    The park/pending slot is the execution source of truth; a missing or
    already-resolved row must never fail the surrounding flow. The write
    is :func:`resolve_external_row`: a row that already left ``pending``
    keeps its status (the rejected write is silent), a missing row is
    silent, and any other error is logged and swallowed.
    """
    if not row_id:
        return
    try:
        await resolve_external_row(
            storage, row_id, status=status, result=result, is_error=is_error,
        )
    except NotFoundError:
        return
    except Exception:  # noqa: BLE001 - audit row is best-effort
        logger.exception("external tool call row flip failed for %r", row_id)


__all__ = ["flip_external_row", "resolve_external_row"]
