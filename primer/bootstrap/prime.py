"""Prime: the default workspace.

Every rule about the default workspace lives in this module (pivot plan P2): which id it has (below), adopting an existing
install's legacy row, and, in later tasks, seeding its ``agents.md`` and the reset.

The default workspace is the one a person meets on first login, and it can never be deleted. Its id is not a
constant: a fresh install creates ``prime``, while an install that predates ``SystemState.default_workspace_id``
keeps the id it already has (``primer``). Code therefore asks :func:`resolve_default_workspace_id` and never compares
against a literal.

Fresh install versus existing install
--------------------------------------

The ensure pass (``primer/bootstrap/seed.py::ensure_default_workspace``) and migration 10 share one rule, so a database
whose migrations were baselined (``auto_bootstrap: false``) ends up where an upgraded one does:

* an id is already recorded: it is the default, whatever else exists;
* nothing is recorded and a ``primer`` row exists: :func:`adopt_legacy_default_workspace` records that id and names the
  row ``prime`` for display. The id is kept (the k3s deployment's workspace, its sessions and its files are keyed by it);
* nothing is recorded and there is no ``primer`` row: a fresh install. The pass creates ``prime`` (or adopts a workspace
  somebody already called ``prime``) and records it.

There is exactly one recorded id, so there can never be two default workspaces: an install that has both a ``primer`` and
a ``prime`` row records ``primer`` and leaves ``prime`` an ordinary workspace.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.bootstrap.defaults import LEGACY_DEFAULT_WORKSPACE_ID, PRIME_WORKSPACE_NAME
from primer.model.except_ import NotFoundError
from primer.model.workspace import Workspace
from primer.storage import raw_generation

logger = logging.getLogger(__name__)


async def resolve_default_workspace_id(storage_provider: Any) -> str | None:
    """The id of the default workspace, or ``None`` when there is none yet.

    The recorded ``SystemState.default_workspace_id`` wins. While it is unset (an install that predates the field,
    before the ensure pass or migration m010 records it) the default is the legacy ``primer`` workspace, but only
    if that row exists: a missing legacy row means there is no default yet, never an invented one. No other row is
    ever taken for the default, whatever it is called, until the id is recorded.

    The recorded id is returned even when its row is gone: naming the default is this function's job; re-creating a
    missing row is the ensure pass's.
    """
    state = await storage_provider.get_system_state()
    recorded = getattr(state, "default_workspace_id", None)
    if recorded:
        return recorded
    if await storage_provider.get_storage(Workspace).get(LEGACY_DEFAULT_WORKSPACE_ID) is not None:
        return LEGACY_DEFAULT_WORKSPACE_ID
    return None


async def record_default_workspace_id(storage_provider: Any, workspace_id: str) -> None:
    """Record ``workspace_id`` as the default workspace, unless an id is already recorded.

    The recorded id is the one fact "which workspace is the default" rests on, so a second recorder (another process
    running the same pass) never replaces it: two processes that chose differently would otherwise leave the install
    with whichever wrote last. The state is read again here, not trusted from the caller's earlier read, to keep the
    window in which that could happen as small as a singleton write allows. Both processes choose the same id on every
    install the pass knows how to handle (the fresh-install id, or the legacy row they both see), so in practice the
    second write is skipped or writes the same value.
    """
    state = await storage_provider.get_system_state()
    if getattr(state, "default_workspace_id", None):
        return
    await storage_provider.set_default_workspace_id(workspace_id)


async def adopt_legacy_default_workspace(storage_provider: Any) -> str | None:
    """Adopt an existing install's ``primer`` workspace as the default; the id it returns is the one now recorded.

    Acts only while no id is recorded AND a ``primer`` row exists, and then once: it names the row ``prime`` for display
    and records the id. Once an id is recorded this never acts again, so a person who later renames the workspace
    keeps their name.

    * **The rename is field-scoped.** One ``patch_if`` of ``name`` alone, guarded on the name as read. The workspace probe
      task rewrites ``phase`` and the ``last_probe_*`` fields of this row on every tick, so a whole-document update from the
      snapshot read here would put stale values back. A guard that no longer matches means a person renamed the workspace
      since the read: their name stays, and the id is still recorded.
    * **The order is rename, then record.** A pass that dies between the two leaves an unrecorded install whose row already
      reads ``prime``, so the next pass writes no name and records the id. The other order would record first and then, on a
      failed rename, never retry it (the id is recorded), stranding the old display name for good.

    Returns ``None`` when there is nothing to adopt (an id is recorded, or there is no ``primer`` row, or the row was deleted
    while the pass was adopting it; in that case nothing is recorded).
    """
    state = await storage_provider.get_system_state()
    if getattr(state, "default_workspace_id", None):
        return None
    workspaces = storage_provider.get_storage(Workspace)
    row = await workspaces.get(LEGACY_DEFAULT_WORKSPACE_ID)
    if row is None:
        return None
    if row.name != PRIME_WORKSPACE_NAME:
        try:
            written = await workspaces.patch_if(
                LEGACY_DEFAULT_WORKSPACE_ID,
                {"name": PRIME_WORKSPACE_NAME},
                where={"name": [raw_generation(row, "name")]},
            )
        except NotFoundError:
            logger.info("prime: the legacy default workspace was deleted while it was being adopted; nothing recorded")
            return None
        if written is None:
            logger.info(
                "prime: the legacy default workspace was renamed while it was being adopted; keeping its name",
                extra={"workspace_id": LEGACY_DEFAULT_WORKSPACE_ID},
            )
    await record_default_workspace_id(storage_provider, LEGACY_DEFAULT_WORKSPACE_ID)
    logger.info(
        "prime: adopted the existing default workspace",
        extra={"workspace_id": LEGACY_DEFAULT_WORKSPACE_ID, "display_name": PRIME_WORKSPACE_NAME},
    )
    return LEGACY_DEFAULT_WORKSPACE_ID


__all__ = [
    "adopt_legacy_default_workspace",
    "record_default_workspace_id",
    "resolve_default_workspace_id",
]
