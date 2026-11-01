"""Migration 23: the install-wide default workspace becomes the first admin's default (access-control Phase 2, Task 8).

Until now ``SystemState.default_workspace_id`` named one default workspace for the whole install (``prime``, or the legacy
``primer``). Each full user now has their own (``User.default_workspace_id``); the existing one is recorded as the first admin's
(the admin ``User`` with the oldest ``created_at``), who already owns it (migration 22). Left alone, so re-running converges:

* no admin, no install default, or no workspace row for it;
* an admin who already has a default;
* a default workspace some other user owns or already records as their default.

The user row is rewritten through a migration-local view with ``extra="allow"``. The users are paged by id (keyset, PERF-T16)
and only the admins and whether someone already records the default are kept; the one write comes after the last page.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import ConfigDict

from primer.bootstrap.prime import resolve_default_workspace_id
from primer.int.storage_provider import StorageProvider
from primer.storage.migrations._rows import iter_rows
from primer.model.common import Identifiable

logger = logging.getLogger(__name__)


class User(Identifiable):  # noqa: N801 - the class name selects the table
    model_config = ConfigDict(extra="allow")

    role: str = "user"
    created_at: datetime | None = None
    disabled: bool = False
    default_workspace_id: str | None = None


class Workspace(Identifiable):  # noqa: N801
    model_config = ConfigDict(extra="allow")

    access: dict[str, Any] | None = None


class M023AdminDefaultWorkspace:
    """Record the install-wide default workspace as the first admin's default."""

    version = 23
    description = "the install-wide default workspace becomes the first admin's default"

    async def apply(self, sp: StorageProvider) -> None:
        users = sp.get_storage(User)
        install_id = await resolve_default_workspace_id(sp)  # a read only, so it may come before the users
        admins: list[User] = []
        taken = False
        async for u in iter_rows(users):
            if u.role == "admin" and not u.disabled and u.created_at is not None:
                admins.append(u)
            taken = taken or (install_id is not None and u.default_workspace_id == install_id)
        if not admins or install_id is None:
            return
        first = min(admins, key=lambda u: u.created_at)
        workspace = await sp.get_storage(Workspace).get(install_id)
        if workspace is None or first.default_workspace_id is not None:
            return
        if taken:
            return
        owners = (workspace.access or {}).get("owners") or []
        if owners and first.id not in owners:
            return
        await users.update(first.model_copy(update={"default_workspace_id": install_id}))
        logger.info("recorded the install default workspace as the first admin's", extra={"workspace_id": install_id, "user": first.id})


__all__ = ["M023AdminDefaultWorkspace"]
