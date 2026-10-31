"""Migration 22: give existing workspaces an owner (access-control Phase 2, Task 9).

Before access control a workspace row recorded no owner: ``Workspace`` rows created by
``workspaces.create_workspace`` before this phase carry an absent or empty ``access``
block (no owners, no grants, not public), and such a row is reachable by nobody but an
admin. This step closes that gap for the whole install, the prime/legacy default
workspace (id ``primer``, whose sessions, files and Kubernetes objects are keyed by the
id) included:

* every such row becomes owned by the first admin (the admin ``User`` with the oldest
  ``created_at``), private;
* with no admin at all the rows are left as they are (admins reach every row
  regardless).

A row that already carries owners, grants or ``public`` is left alone, so re-running
converges. The access block is written as a literal dict (members derived here, frozen
with this step) through a migration-local view with ``extra="allow"``, read in full
before any write, as ``m021`` does.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200


class _View(Identifiable):
    model_config = ConfigDict(extra="allow")

    access: dict[str, Any] | None = None


# The class name selects the table (the storage layer lowercases it).
class Workspace(_View):  # noqa: N801
    pass


class User(Identifiable):  # noqa: N801
    model_config = ConfigDict(extra="allow")

    role: str = "user"
    created_at: datetime | None = None
    disabled: bool = False


def _owned_by(user_id: str) -> dict[str, Any]:
    return {"owners": [user_id], "grants": {}, "public": False, "public_tools": [], "members": [f"u:{user_id}"], "rev": 0}


def _unset(access: dict[str, Any] | None) -> bool:
    return not access or not (access.get("owners") or access.get("grants") or access.get("public"))


async def _read_all(store) -> list[Any]:
    rows: list[Any] = []
    offset = 0
    while True:
        page = await store.list(OffsetPage(offset=offset, length=_PAGE))
        rows.extend(page.items)
        if len(page.items) < _PAGE:
            return rows
        offset += _PAGE


class M022WorkspaceOwnership:
    """Stamp an owner on every workspace that has none."""

    version = 22
    description = "give existing workspaces an owner"

    async def apply(self, sp: StorageProvider) -> None:
        admins = [u for u in await _read_all(sp.get_storage(User)) if u.role == "admin" and not u.disabled and u.created_at is not None]
        first_admin = min(admins, key=lambda u: u.created_at).id if admins else None
        if first_admin is None:
            return
        store = sp.get_storage(Workspace)
        count = 0
        for row in await _read_all(store):
            if not _unset(row.access):
                continue
            await store.update(row.model_copy(update={"access": _owned_by(first_admin)}))
            count += 1
        if count:
            logger.info("gave existing workspaces an owner", extra={"rows": count, "owner": first_admin})


__all__ = ["M022WorkspaceOwnership"]
