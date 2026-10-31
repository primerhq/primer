"""Migration 27: harnesses get an owner; what they installed copies their access (access-control Phase 3, Task 6).

Before access control a harness recorded no owner. A harness whose ``access`` block is absent or empty (no owners, no grants, not
public) is owned by the first admin (the enabled admin ``User`` with the oldest ``created_at``), private; with no such admin it is left
as it is (admins reach every row regardless). Then every agent, graph, toolset and collection with a ``harness_id`` takes a copy of
that harness's access (plan P3 Decision 1: documents follow their collection, so they carry none). A row whose harness is gone keeps
what it has. Re-running converges: a row already equal to its harness's access is not written.

The blocks are written as literal dicts through migration-local views with ``extra="allow"``, read in full before any write, as
``m021`` does.
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
    harness_id: str | None = None


# The class name selects the table (the storage layer lowercases it).
class Harness(_View):  # noqa: N801
    pass


class Agent(_View):  # noqa: N801
    pass


class Graph(_View):  # noqa: N801
    pass


class Toolset(_View):  # noqa: N801
    pass


class Collection(_View):  # noqa: N801
    pass


class User(Identifiable):  # noqa: N801
    model_config = ConfigDict(extra="allow")

    role: str = "user"
    disabled: bool = False
    created_at: datetime | None = None


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


class M027HarnessOwnership:
    """Own every unset harness by the first admin; copy each harness's access onto the rows it installed."""

    version = 27
    description = "harnesses get an owner; installed rows copy their harness's access"

    async def apply(self, sp: StorageProvider) -> None:
        admins = [
            u for u in await _read_all(sp.get_storage(User))
            if u.role == "admin" and not u.disabled and u.created_at is not None
        ]
        first_admin = min(admins, key=lambda u: u.created_at).id if admins else None
        harnesses = sp.get_storage(Harness)
        by_id: dict[str, dict[str, Any] | None] = {}
        owned = 0
        for row in await _read_all(harnesses):
            access = row.access
            if _unset(access) and first_admin is not None:
                access = _owned_by(first_admin)
                await harnesses.update(row.model_copy(update={"access": access}))
                owned += 1
            by_id[row.id] = access
        copied = 0
        for model in (Agent, Graph, Toolset, Collection):
            store = sp.get_storage(model)
            for row in await _read_all(store):
                access = by_id.get(row.harness_id) if row.harness_id else None
                if not access or row.access == access:
                    continue
                await store.update(row.model_copy(update={"access": access}))
                copied += 1
        if owned or copied:
            logger.info("harness ownership applied", extra={"harnesses": owned, "installed_rows": copied, "owner": first_admin})


__all__ = ["M027HarnessOwnership"]
