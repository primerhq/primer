"""Migration 25: give existing collections an owner; the system wiki becomes public (access-control Phase 3, Task 3).

Before access control a collection recorded no owner. A row whose ``access`` block is absent or empty (no owners, no
grants, not public) gets one, by the rules of plan P3 Decision 7:

* ``system`` (the platform wiki) becomes public, whatever its other sharing: every full user reads it and the wiki tools
  depend on it; its system flag keeps it read-only;
* an ``_internal_*`` collection stays as it is (no owners, not public): admin-only;
* any other unset row is owned by the first admin (the enabled admin ``User`` with the oldest ``created_at``), private;
* with no such admin those rows are left as they are (admins reach every row regardless).

A row that already carries owners, grants or ``public`` is left alone (``system`` aside, which only gains ``public``), so
re-running converges. The access block is written as a literal dict (members derived here, frozen with this step)
through a migration-local view with ``extra="allow"``, read in full before any write, as ``m021`` does.
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

#: Frozen with this step: the platform wiki's id and the internal collections' prefix.
_SYSTEM_ID = "system"
_INTERNAL_PREFIX = "_internal_"


class _View(Identifiable):
    model_config = ConfigDict(extra="allow")

    access: dict[str, Any] | None = None


# The class name selects the table (the storage layer lowercases it).
class Collection(_View):  # noqa: N801
    pass


class User(Identifiable):  # noqa: N801
    model_config = ConfigDict(extra="allow")

    role: str = "user"
    disabled: bool = False
    created_at: datetime | None = None


def _owned_by(user_id: str) -> dict[str, Any]:
    return {"owners": [user_id], "grants": {}, "public": False, "public_tools": [], "members": [f"u:{user_id}"], "rev": 0}


def _made_public(access: dict[str, Any] | None) -> dict[str, Any]:
    base = {"owners": [], "grants": {}, "public": False, "public_tools": [], "members": [], "rev": 0, **(access or {})}
    return {**base, "public": True, "members": sorted({*base["members"], "public"})}


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


class M025CollectionOwnership:
    """Stamp the first admin as owner of every unset collection; make ``system`` public; leave ``_internal_*`` admin-only."""

    version = 25
    description = "give existing collections an owner; the system wiki becomes public"

    async def apply(self, sp: StorageProvider) -> None:
        admins = [
            u for u in await _read_all(sp.get_storage(User))
            if u.role == "admin" and not u.disabled and u.created_at is not None
        ]
        first_admin = min(admins, key=lambda u: u.created_at).id if admins else None
        store = sp.get_storage(Collection)
        owned = 0
        for row in await _read_all(store):
            if row.id == _SYSTEM_ID:
                if not (row.access or {}).get("public"):
                    await store.update(row.model_copy(update={"access": _made_public(row.access)}))
                    logger.info("made the system collection public")
                continue
            if row.id.startswith(_INTERNAL_PREFIX) or not _unset(row.access) or first_admin is None:
                continue
            await store.update(row.model_copy(update={"access": _owned_by(first_admin)}))
            owned += 1
        if owned:
            logger.info("gave existing collections an owner", extra={"rows": owned, "owner": first_admin})


__all__ = ["M025CollectionOwnership"]
