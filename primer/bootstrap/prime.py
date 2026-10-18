"""Prime: the default workspace.

Every rule about the default workspace lives in this module (pivot plan P2): which id it has (below), and, in later
tasks, adopting an existing install's legacy row, seeding its main session and its ``agents.md``, and the reset.

The default workspace is the one a person meets on first login, and it can never be deleted. Its id is not a
constant: a fresh install creates ``prime``, while an install that predates ``SystemState.default_workspace_id``
keeps the id it already has (``primer``). Code therefore asks :func:`resolve_default_workspace_id` and never compares
against a literal.
"""

from __future__ import annotations

from typing import Any

from primer.bootstrap.defaults import LEGACY_DEFAULT_WORKSPACE_ID
from primer.model.workspace import Workspace


async def resolve_default_workspace_id(storage_provider: Any) -> str | None:
    """The id of the default workspace, or ``None`` when there is none yet.

    The recorded ``SystemState.default_workspace_id`` wins. While it is unset (an install that predates the field,
    before the ensure pass or migration m009 records it) the default is the legacy ``primer`` workspace, but only
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


__all__ = ["resolve_default_workspace_id"]
