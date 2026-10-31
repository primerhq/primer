"""Migration 27: MCP OAuth token sets become per person (access-control plan P5 Task 2, Decision 4).

Before this step a toolset had ONE token row, stored under the toolset id, used by every caller. A browser (``authorization_code``) sign-in now
belongs to one person, stored under ``token_key(toolset, user)`` and naming both in ``toolset_id`` / ``user_id``; a client-credentials sign-in stays
the shared row under the bare toolset id. Each token row that names no ``toolset_id`` yet (its id is then the toolset id):

* a browser row moves to its heir: the user whose username (or id) is its ``connected_by`` (the admin who connected it), else the first admin (the
  admin ``User`` with the oldest ``created_at``). Not the toolset's owner: the token is an upstream account, and handing it to someone who never held
  it would be an escalation through a credential. The copy keeps every stored field (the sealed tokens as their ciphertext, the version, the lease);
* a browser row with no heir (an install with no users: the auth-disabled shape, whose system person keeps the shared row) and a client-credentials
  row stay under the bare id, stamped with ``toolset_id`` and ``user_id = None``.

Every pending sign-in is dropped (they last ten minutes, and the old ones name no person). Registered clients are primer's, not a person's, and stay.

What the step reads is frozen here: the three table names (each view's class name selects its table) and the key function. The views keep every
stored field (``extra="allow"``), so a row the live models may reject is still read and copied whole; no sealed value is opened or logged. Every row
is read before any is written. A row that already names its toolset is done, so a second run changes nothing; a run that died after writing the
heir's copy and before deleting the bare row finishes the delete and leaves the copy as it is.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.storage import CursorPage

logger = logging.getLogger(__name__)

_PAGE = 200


class _View(Identifiable):
    model_config = ConfigDict(extra="allow")


# The class name selects the table (the storage layer lowercases it).
class McpOAuthTokenSet(_View):  # noqa: N801
    grant: str | None = None
    toolset_id: str | None = None
    connected_by: str | None = None


class McpOAuthPendingSignIn(_View):  # noqa: N801
    pass


class User(_View):  # noqa: N801
    username: str | None = None
    role: str = "user"
    created_at: datetime | None = None


def _token_key(toolset_id: str, user_id: str) -> str:
    """Frozen copy of :func:`primer.toolset.oauth.store.token_key` as it was when this step shipped."""
    return "u-" + hashlib.sha256(f"{toolset_id}\0{user_id}".encode()).hexdigest()


async def _all_rows(store) -> list[Identifiable]:
    rows: list[Identifiable] = []
    cursor: str | None = None
    while True:
        page = await store.list(CursorPage(cursor=cursor, length=_PAGE))
        rows.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return rows


class M027McpOAuthPerUser:
    """Move each browser token set to the person who connected it (else the first admin), stamp every token row, drop pending sign-ins."""

    version = 27
    description = "MCP OAuth browser token sets become the sign-in of the person who connected them; pending sign-ins are dropped"

    async def apply(self, sp: StorageProvider) -> None:
        tokens = sp.get_storage(McpOAuthTokenSet)
        pending = sp.get_storage(McpOAuthPendingSignIn)
        users = await _all_rows(sp.get_storage(User))
        token_rows = await _all_rows(tokens)
        pending_rows = await _all_rows(pending)

        by_name = {u.username: u.id for u in users if u.username}
        by_id = {u.id for u in users}
        admins = [u for u in users if u.role == "admin" and u.created_at is not None]
        first_admin = min(admins, key=lambda u: u.created_at).id if admins else None

        moved = stamped = 0
        for row in token_rows:
            if row.toolset_id is not None:
                continue
            assert row.id is not None
            heir = None
            if row.grant != "client_credentials":
                named = row.connected_by
                heir = by_name.get(named) or (named if named in by_id else None) or first_admin
            if heir is None:
                await tokens.update(row.model_copy(update={"toolset_id": row.id, "user_id": None}))
                stamped += 1
                continue
            try:
                await tokens.create(row.model_copy(update={"id": _token_key(row.id, heir), "toolset_id": row.id, "user_id": heir}))
            except ConflictError:
                pass                                    # an earlier run wrote the copy and died before the delete
            try:
                await tokens.delete(row.id)
            except NotFoundError:
                pass
            moved += 1
        dropped = 0
        for row in pending_rows:
            try:
                await pending.delete(row.id)
            except NotFoundError:
                continue
            dropped += 1
        if moved or stamped or dropped:
            # Counts only: a token row's new id is a hash of a user id, and a pending row's id a hash of a state.
            logger.info("MCP OAuth sign-ins are per person now", extra={"moved": moved, "stamped": stamped, "pending_dropped": dropped})


__all__ = ["M027McpOAuthPerUser"]
