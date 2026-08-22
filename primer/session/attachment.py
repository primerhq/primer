"""Attach / heartbeat / detach helpers over ``ClientAttachment`` rows.

Shared by the API endpoints (write path) and the worker's executor build
(read path), so both agree on what "a client is attached" means.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from primer.model.client_attachment import ClientAttachment
from primer.model.storage import OffsetPage
from primer.storage.q import Q

# Short by design: a browser that dies silently must stop counting fast.
# The console heartbeats at a third of this.
ATTACH_TTL_SECONDS = 30.0

_PAGE = 200


async def _rows_for_session(storage: Any, session_id: str) -> list[ClientAttachment]:
    q = Q(ClientAttachment).where("session_id", session_id)
    page = await storage.find(q.build(), OffsetPage(offset=0, length=_PAGE))
    return list(page.items)


async def live_attachments(
    storage: Any, session_id: str, *, now: datetime | None = None
) -> list[ClientAttachment]:
    """Non-expired attachments for ``session_id``; expired rows are swept.

    Sweeping on read mirrors the external-tool timeout materialisation
    (primer/api/routers/external_tools.py): there is no background job, so
    the reader is the one that notices.
    """
    moment = now or datetime.now(UTC)
    live: list[ClientAttachment] = []
    for row in await _rows_for_session(storage, session_id):
        if row.expires_at > moment:
            live.append(row)
        else:
            await storage.delete(row.id)
    return live


async def attach_or_refresh(
    storage: Any,
    *,
    workspace_id: str,
    session_id: str,
    client_id: str,
    last_seq: int,
    now: datetime | None = None,
) -> ClientAttachment:
    """Create the attachment, or extend an existing live one.

    A refresh NEVER moves ``attached_seq``: the fence belongs to the
    attachment, not to the heartbeat. A re-attach after expiry is a NEW
    attachment and re-fences at the session's current ``last_seq``.
    """
    moment = now or datetime.now(UTC)
    expires = moment + timedelta(seconds=ATTACH_TTL_SECONDS)
    for row in await live_attachments(storage, session_id, now=moment):
        if row.client_id == client_id:
            row.expires_at = expires
            return await storage.update(row)
    return await storage.create(
        ClientAttachment(
            workspace_id=workspace_id,
            session_id=session_id,
            client_id=client_id,
            attached_seq=last_seq,
            expires_at=expires,
            created_at=moment,
        )
    )


async def detach(storage: Any, *, session_id: str, client_id: str) -> bool:
    """Remove one client's attachment. True iff a row was removed."""
    for row in await _rows_for_session(storage, session_id):
        if row.client_id == client_id:
            await storage.delete(row.id)
            return True
    return False


__all__ = [
    "ATTACH_TTL_SECONDS",
    "attach_or_refresh",
    "detach",
    "live_attachments",
]
