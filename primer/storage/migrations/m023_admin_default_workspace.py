"""Migration 23: the install-wide default workspace becomes the first admin's default (access-control Phase 2, Task 8).

Until now ``SystemState.default_workspace_id`` named one default workspace for the whole install (``prime``, or the legacy
``primer``). Each full user now has their own (``User.default_workspace_id``); the existing one is recorded as the first admin's
(the admin ``User`` with the oldest ``created_at``), who already owns it (migration 22). Left alone, so re-running converges:

* no admin, no install default, or no workspace row for it;
* an admin who already has a default;
* a default workspace some other user owns or already records as their default.

The user row is rewritten through a migration-local view with ``extra="allow"``, read in full first, as m021 and m022 do.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import ConfigDict

from primer.bootstrap.prime import resolve_default_workspace_id
from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200


class User(Identifiable):  # noqa: N801 - the class name selects the table
    model_config = ConfigDict(extra="allow")

    role: str = "user"
    created_at: datetime | None = None
    disabled: bool = False
    default_workspace_id: str | None = None


class Workspace(Identifiable):  # noqa: N801
    model_config = ConfigDict(extra="allow")

    access: dict[str, Any] | None = None


async def _read_all(store) -> list[Any]:
    rows: list[Any] = []
    offset = 0
    while True:
        page = await store.list(OffsetPage(offset=offset, length=_PAGE))
        rows.extend(page.items)
        if len(page.items) < _PAGE:
            return rows
        offset += _PAGE


class M023AdminDefaultWorkspace:
    """Record the install-wide default workspace as the first admin's default."""

    version = 23
    description = "the install-wide default workspace becomes the first admin's default"

    async def apply(self, sp: StorageProvider) -> None:
        users = sp.get_storage(User)
        rows = await _read_all(users)
        admins = [u for u in rows if u.role == "admin" and not u.disabled and u.created_at is not None]
        install_id = await resolve_default_workspace_id(sp)
        if not admins or install_id is None:
            return
        first = min(admins, key=lambda u: u.created_at)
        workspace = await sp.get_storage(Workspace).get(install_id)
        if workspace is None or first.default_workspace_id is not None:
            return
        if any(u.default_workspace_id == install_id for u in rows):
            return
        owners = (workspace.access or {}).get("owners") or []
        if owners and first.id not in owners:
            return
        await users.update(first.model_copy(update={"default_workspace_id": install_id}))
        logger.info("recorded the install default workspace as the first admin's", extra={"workspace_id": install_id, "user": first.id})


__all__ = ["M023AdminDefaultWorkspace"]
