"""Migration 29: MCP OAuth token sets become per person (access-control plan P5 Task 2, Decision 4).

Before this step a toolset had ONE token row, stored under the toolset id, used by every caller. A browser (``authorization_code``) sign-in now
belongs to one person, stored under ``token_key(toolset, user)`` and naming both in ``toolset_id`` / ``user_id``; a client-credentials sign-in stays
the shared row under the bare toolset id. Each token row that names no ``toolset_id`` yet (its id is then the toolset id):

* a browser row is COPIED to its heir (and the bare row stays as the shared copy, stamped, so an auth-disabled install that once had users keeps its sign-ins): the user whose id is its ``connected_by``, else the ENABLED admin whose username it is (the admin who connected
  it; a name now held by a plain user, or by a disabled admin, does not count: review finding 3), else the first enabled admin (the enabled admin
  ``User`` with the oldest ``created_at``). Not the toolset's owner: the token is an upstream account, and handing it to someone who never held it
  would be an escalation through a credential. The copy keeps every stored field (the sealed tokens as their ciphertext, the version, the lease);
* a browser row with no heir (no enabled admin: the auth-disabled shape has no users at all, and its system person keeps the shared row) and a
  client-credentials row stay under the bare id, stamped with ``toolset_id`` and ``user_id = None``.

Every pending sign-in is dropped (they last ten minutes, and the old ones name no person). Registered clients are primer's, not a person's, and stay.

What the step reads is frozen here: the three table names (each view's class name selects its table) and the key function. The views keep every
stored field (``extra="allow"``), so a row the live models may reject is still read and copied whole; no sealed value is opened or logged. The users
are read first (their ids and the admins are kept). Token rows are then paged by id (keyset, PERF-T16) selecting ``toolset_id IS NULL``, exactly the
rows the step acts on: the heir's copy it creates (an id after the cursor) and the bare row it stamps both name their toolset, so neither is ever
paged. Pending rows are paged by id and each is deleted after it is read, behind the cursor. A row that already names its toolset is done, so a second run changes nothing; a run that died after writing the
heir's copy and before stamping the bare row finishes the stamp and leaves the copy as it is.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.storage.migrations._rows import iter_rows
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.storage.q import Q

logger = logging.getLogger(__name__)


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
    disabled: bool = False
    created_at: datetime | None = None


def _token_key(toolset_id: str, user_id: str) -> str:
    """Frozen copy of :func:`primer.toolset.oauth.store.token_key` as it was when this step shipped."""
    return "u-" + hashlib.sha256(f"{toolset_id}\0{user_id}".encode()).hexdigest()


class M029McpOAuthPerUser:
    """Move each browser token set to the person who connected it (else the first admin), stamp every token row, drop pending sign-ins."""

    version = 29
    description = "MCP OAuth browser token sets become the sign-in of the person who connected them; pending sign-ins are dropped"

    async def apply(self, sp: StorageProvider) -> None:
        tokens = sp.get_storage(McpOAuthTokenSet)
        pending = sp.get_storage(McpOAuthPendingSignIn)
        by_id: set[str] = set()
        admins: list[User] = []
        async for u in iter_rows(sp.get_storage(User)):
            by_id.add(u.id)
            if u.role == "admin" and not u.disabled:
                admins.append(u)
        by_name = {u.username: u.id for u in admins if u.username}
        dated = [u for u in admins if u.created_at is not None]
        first_admin = min(dated, key=lambda u: u.created_at).id if dated else None

        moved = stamped = 0
        async for row in iter_rows(tokens, predicate=Q(McpOAuthTokenSet).where_null("toolset_id").build()):
            assert row.id is not None
            heir = None
            if row.grant != "client_credentials":
                named = row.connected_by
                heir = (named if named in by_id else None) or by_name.get(named) or first_admin
            if heir is None:
                await tokens.update(row.model_copy(update={"toolset_id": row.id, "user_id": None}))
                stamped += 1
                continue
            try:
                await tokens.create(row.model_copy(update={"id": _token_key(row.id, heir), "toolset_id": row.id, "user_id": heir}))
            except ConflictError:
                pass                                    # an earlier run wrote the copy and died before the delete
            # The bare id stays, stamped as the shared row: an auth-disabled install that once had users acts as the system person, which reads
            # only that row, and would otherwise find every OAuth toolset signed out (GR-14). The heir's copy is the person's own sign-in.
            await tokens.update(row.model_copy(update={"toolset_id": row.id, "user_id": None}))
            moved += 1
        dropped = 0
        async for row in iter_rows(pending):
            try:
                await pending.delete(row.id)
            except NotFoundError:
                continue
            dropped += 1
        if moved or stamped or dropped:
            # Counts only: a token row's new id is a hash of a user id, and a pending row's id a hash of a state.
            logger.info("MCP OAuth sign-ins are per person now", extra={"moved": moved, "stamped": stamped, "pending_dropped": dropped})


__all__ = ["M029McpOAuthPerUser"]
