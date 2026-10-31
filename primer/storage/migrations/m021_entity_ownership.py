"""Migration 21: give existing agents, graphs, toolsets, workspace templates and channels an owner (access-control spec 8).

Before access control nothing recorded who created these rows. A row whose ``access`` block is absent or empty (no
owners, no grants, not public) gets one:

* a seeded row (the agents ``operator``, ``builder``, ``planner``, ``explorer``, ``tool-runner`` and the template
  ``local-default``) becomes public with no owner, so every user may use it and admins manage it;
* any other row is owned by the first admin (the admin ``User`` with the oldest ``created_at``), private;
* with no admin at all the row is left as it is (admins reach every row regardless).

A row that already carries owners, grants or ``public`` is left alone, so re-running converges. The access block is
written as a literal dict (members derived here, frozen with this step) through migration-local views with
``extra="allow"``, read in full before any write, as ``m019`` does.
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

#: Frozen with this step: the seeded rows as of migration 21.
_SEEDED: dict[str, frozenset[str]] = {
    "agent": frozenset({"operator", "builder", "planner", "explorer", "tool-runner"}),
    "workspacetemplate": frozenset({"local-default"}),
}


class _View(Identifiable):
    model_config = ConfigDict(extra="allow")

    access: dict[str, Any] | None = None


# The class name selects the table (the storage layer lowercases it).
class Agent(_View):  # noqa: N801
    pass


class Graph(_View):  # noqa: N801
    pass


class Toolset(_View):  # noqa: N801
    pass


class WorkspaceTemplate(_View):  # noqa: N801
    pass


class Channel(_View):  # noqa: N801
    pass


class User(Identifiable):  # noqa: N801
    model_config = ConfigDict(extra="allow")

    role: str = "user"
    created_at: datetime | None = None
    disabled: bool = False


def _public() -> dict[str, Any]:
    return {"owners": [], "grants": {}, "public": True, "public_tools": [], "members": ["public"], "rev": 0}


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


class M021EntityOwnership:
    """Stamp an owner (or public, for seeded rows) on every agent, graph, toolset, template and channel that has none."""

    version = 21
    description = "give existing agents, graphs, toolsets, workspace templates and channels an owner"

    async def apply(self, sp: StorageProvider) -> None:
        admins = [u for u in await _read_all(sp.get_storage(User)) if u.role == "admin" and not u.disabled and u.created_at is not None]
        first_admin = min(admins, key=lambda u: u.created_at).id if admins else None
        stamped: dict[str, int] = {}
        for view in (Agent, Graph, Toolset, WorkspaceTemplate, Channel):
            table = view.__name__.lower()
            store = sp.get_storage(view)
            count = 0
            for row in await _read_all(store):
                if not _unset(row.access):
                    continue
                if row.id in _SEEDED.get(table, ()):
                    access = _public()
                elif first_admin is not None:
                    access = _owned_by(first_admin)
                else:
                    continue
                await store.update(row.model_copy(update={"access": access}))
                count += 1
            if count:
                stamped[table] = count
        if stamped:
            logger.info("gave existing rows an owner", extra={"rows": stamped, "owner": first_admin})


__all__ = ["M021EntityOwnership"]
