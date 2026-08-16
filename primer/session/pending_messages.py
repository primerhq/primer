"""Deferred-steer queue for sessions (port of the chat pending drain).

The routing rule: a steer arriving while the session already has a
non-terminal turn is stored here, with NO USER_INPUT record written, and
realized at the drain checkpoint instead. Allocating a seq at receipt is
what collided with the in-flight turn's assistant_token seqs on the chat
surface (primer/model/chats.py:280-308), so these rows carry none.

Realization goes through :func:`wake_session` rather than writing the
record directly, so the one canonical persist-and-wake path stays
canonical: USER_INPUT record, title derivation, claimable flip, and the
scheduler pulse all keep happening in exactly one place.

Exactly ONE row is realized per checkpoint. Draining the whole queue at
once would write several user messages against a single turn and break
the 1:1 user_input-to-terminal pairing the drain counts on
(primer/session/turns.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.model.workspace_session import PendingSessionMessage
from primer.session.enqueue import wake_session


async def store_pending_steer(
    *,
    storage_provider: Any,
    session_id: str,
    text: str,
    attribution: dict | None = None,
    client_msg_id: str | None = None,
) -> PendingSessionMessage:
    """Queue a follow-up steer without touching the message log."""
    now = datetime.now(UTC)
    row = PendingSessionMessage(
        id=f"{session_id}:pending:{now.isoformat()}:{uuid.uuid4().hex[:8]}",
        session_id=session_id,
        parts=[{"type": "text", "text": text}],
        attribution=attribution,
        client_msg_id=client_msg_id,
        enqueued_at=now,
        created_at=now,
    )
    await storage_provider.get_storage(PendingSessionMessage).create(row)
    return row


async def realize_next_pending(
    *,
    storage_provider: Any,
    workspace_id: str,
    session_id: str,
    wake_deps: Any,
) -> bool:
    """Realize the oldest queued steer into a real turn.

    Returns True when a row was realized and the session woken. The row
    is deleted before the wake so a crash between the two loses the
    follow-up rather than replaying it forever.
    """
    storage = storage_provider.get_storage(PendingSessionMessage)
    page = await storage.find(
        Predicate(
            left=FieldRef(name="session_id"), op=Op.EQ,
            right=Value(value=session_id),
        ),
        OffsetPage(offset=0, length=1),
        order_by=[
            OrderBy(field="enqueued_at", direction="asc"),
            OrderBy(field="id", direction="asc"),
        ],
    )
    rows = list(page.items)
    if not rows:
        return False
    row = rows[0]
    text = "\n".join(
        p.get("text", "") for p in row.parts
        if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
    )
    await storage.delete(row.id)
    if not text:
        # Reaped rather than left at the head of the queue, where it
        # would block every later follow-up behind an empty wake.
        return False
    await wake_session(
        workspace_id=workspace_id,
        session_id=session_id,
        instruction=text,
        deps=wake_deps,
    )
    return True


__all__ = ["realize_next_pending", "store_pending_steer"]
