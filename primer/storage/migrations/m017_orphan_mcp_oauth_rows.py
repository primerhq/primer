"""Migration 17: forget the MCP OAuth sign-ins of toolsets that no longer exist (ticket 01a16a36, L2 of the #852 review).

Before #852 the system ``delete_toolset`` tool and the harness uninstall and sync deleted a toolset's row and left its shared sign-in behind: the
token row and the registered client (both stored under the toolset id) and the pending browser sign-ins (stored under a hash of their state, naming
the toolset in ``toolset_id``). A toolset created again under the same id came back connected on the old tokens, with no admin connecting it
(audit G4-02). #852 made every delete path forget the sign-in; this step removes what the old code left:

* a token row or a registered client whose id names no stored toolset is deleted;
* a pending sign-in whose ``toolset_id`` names no stored toolset is deleted;
* a row whose toolset exists is never touched, whatever its state, and a pending sign-in with no ``toolset_id`` is left as it is (it names no
  toolset, so it is nobody's orphan to decide).

What the step reads is frozen here, as it was when it shipped: the four table names (each view's class name selects its table) and where each OAuth
row names its toolset. The views keep every stored field (``extra="allow"``), so a row the live models may reject is still read, and only ids and
``toolset_id`` are looked at: no sealed value is opened, logged or rewritten. Every row is read before any is deleted, so the paging never skips a
row, and a row already gone is skipped (``NotFoundError``): a second run, or a run that died half way, deletes nothing more than the first would.
The toolset is looked up right before each delete. The step runs at boot, before this process serves, but in a rolling deploy a pod still on the
old build may create a toolset meanwhile: only a sign-in made in the gap between that read and the delete could go, and it would need an admin to
connect it again, which is what a toolset created under a deleted one's id needs anyway.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import NotFoundError
from primer.model.storage import CursorPage

logger = logging.getLogger(__name__)

_PAGE = 200


class Toolset(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view of a toolset row: only its id is read."""

    model_config = ConfigDict(extra="allow")


class McpOAuthTokenSet(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view of a toolset's tokens. The id is the toolset id."""

    model_config = ConfigDict(extra="allow")


class McpOAuthClient(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view of a toolset's registered client. The id is the toolset id."""

    model_config = ConfigDict(extra="allow")


class McpOAuthPendingSignIn(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view of a started browser sign-in. The id is a hash of its state; ``toolset_id`` names the toolset."""

    model_config = ConfigDict(extra="allow")

    toolset_id: str | None = None


#: ``(table view, the toolset a row belongs to)``, in the order they are swept.
_OAUTH_TABLES: tuple[tuple[type[Identifiable], Callable[[Identifiable], object]], ...] = (
    (McpOAuthTokenSet, lambda row: row.id),
    (McpOAuthClient, lambda row: row.id),
    (McpOAuthPendingSignIn, lambda row: getattr(row, "toolset_id", None)),
)


async def _all_rows(store) -> list[Identifiable]:
    rows: list[Identifiable] = []
    cursor: str | None = None
    while True:
        page = await store.list(CursorPage(cursor=cursor, length=_PAGE))
        rows.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return rows


class M017OrphanMcpOAuthRows:
    """Delete the MCP OAuth tokens, registered clients and pending sign-ins whose toolset no longer exists."""

    version = 17
    description = "Delete the MCP OAuth tokens, registered clients and pending sign-ins of toolsets that no longer exist"

    async def apply(self, sp: StorageProvider) -> None:
        toolsets = sp.get_storage(Toolset)
        removed: dict[str, int] = {}
        for view, owner in _OAUTH_TABLES:
            store = sp.get_storage(view)
            count = 0
            for row in await _all_rows(store):
                toolset_id = owner(row)
                if not isinstance(toolset_id, str) or not toolset_id:
                    continue
                # Read right before the delete, never from an answer kept since an earlier row: the window in which a toolset created on
                # another pod (a rolling deploy) could lose a sign-in made meanwhile is one read and one delete wide.
                if await toolsets.get(toolset_id) is not None:
                    continue
                assert row.id is not None
                try:
                    await store.delete(row.id)
                except NotFoundError:
                    continue
                count += 1
            removed[view.__name__] = count
        if any(removed.values()):
            # Counts only: a row id of the pending table is a hash of a state, and none of them is needed to act on this line.
            logger.info("forgot the MCP OAuth sign-ins of deleted toolsets", extra={"removed": removed})


__all__ = ["M017OrphanMcpOAuthRows"]
