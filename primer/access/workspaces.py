"""Access on workspaces and their sessions (access-control spec 6.2, 6.7; plan P2 Task 1).

``workspace_level`` resolves a person's level on one workspace row;
``require_workspace`` and ``require_session`` enforce a minimum level with
the Phase 1 answers (a missing row, or a row the person holds no level on,
answers exactly like a missing one; an insufficient level answers
``AccessDeniedError``). ``readable_workspace_ids`` is the id set a person's
cross-workspace list queries filter on (``None`` for admins: no filter). A
session inherits the level of the workspace it lives in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.access.person import Person
from primer.access.policy import level_of, readable_ids, require
from primer.model.access import AccessLevel
from primer.model.except_ import NotFoundError
from primer.model.storage import Op, Predicate
from primer.model.workspace import Workspace
from primer.model.workspace_session import WorkspaceSession

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


async def _workspace(sp: "StorageProvider", workspace_id: str) -> Workspace | None:
    """Fetch the workspace row, or ``None`` (the missing-row answer)."""
    return await sp.get_storage(Workspace).get(workspace_id)


async def workspace_level(
    sp: "StorageProvider",
    person: Person,
    workspace_id: str,
) -> AccessLevel | None:
    """The level ``person`` holds on the workspace, or ``None``.

    ``None`` when the row is missing or the person holds no level on it
    (spec 9: an out-of-access workspace is indistinguishable from a
    missing one).
    """
    row = await _workspace(sp, workspace_id)
    if row is None:
        return None
    return level_of(person, row.access)


async def require_workspace(
    sp: "StorageProvider",
    person: Person,
    workspace_id: str,
    need: AccessLevel,
) -> Workspace:
    """Enforce that ``person`` holds at least ``need`` on the workspace.

    A missing id and an id the person holds no level on answer the same
    ``NotFoundError`` (the missing-row text); a level below ``need``
    answers ``AccessDeniedError`` (Phase 1 ``require``). Returns the row.
    """
    what = f"Workspace {workspace_id!r}"
    row = await _workspace(sp, workspace_id)
    if row is None:
        raise NotFoundError(f"{what} does not exist")
    require(person, row.access, need, what=what)
    return row


async def require_session(
    sp: "StorageProvider",
    person: Person,
    session_id: str,
    need: AccessLevel,
) -> WorkspaceSession:
    """Enforce that ``person`` holds at least ``need`` on the session's workspace.

    A session inherits the access of the workspace it lives in, and the
    refusal answers with the SESSION's missing-row text (spec 9): an
    unreadable workspace must be indistinguishable from a missing session
    id. Returns the session row.
    """
    what = f"Session {session_id!r}"
    session = await sp.get_storage(WorkspaceSession).get(session_id)
    if session is None:
        raise NotFoundError(f"{what} does not exist")
    if person.is_admin:
        return session  # admins read everything, a session whose workspace is gone (workspace_lost) included
    workspace = await _workspace(sp, session.workspace_id)
    # A dangling workspace id grants nothing: answer like a missing session.
    if workspace is None:
        raise NotFoundError(f"{what} does not exist")
    require(person, workspace.access, need, what=what)
    return session


async def readable_workspace_ids(
    sp: "StorageProvider",
    person: Person,
    need: AccessLevel = AccessLevel.READ,
) -> list[str] | None:
    """The ids of the workspaces ``person`` holds at least ``need`` on.

    ``None`` for admins (they see everything: no filter). Otherwise the
    ids from a ``visible_predicate`` find over ``Workspace``, kept only
    while the held level reaches ``need`` (public rows count as read).
    """
    return await readable_ids(sp.get_storage(Workspace), person, need)


async def scope_to_readable_workspaces(
    sp: "StorageProvider",
    person: Person,
    predicate: Predicate | None = None,
    *,
    field: str = "workspace_id",
) -> tuple[bool, Predicate | None]:
    """``(nothing_readable, predicate)`` for a cross-workspace list: ``predicate`` ANDed with ``field IN <readable workspace ids>``.

    Admins get ``predicate`` back unchanged (``None`` when they passed none: no filter). A person who reads no workspace gets
    ``(True, ...)``: the caller answers an empty page without a query. The filter is part of the query, so a page and its total are
    counted over what the person may read (plan P2 Task 5; a post-filter would leak the others' count and starve the window).
    """
    from primer.model.storage import FieldRef, Value

    ids = await readable_workspace_ids(sp, person)
    if ids is None:
        return False, predicate
    if not ids:
        return True, predicate
    scoped = Predicate(left=FieldRef(name=field), op=Op.IN, right=Value(value=ids))
    return False, scoped if predicate is None else Predicate(left=scoped, op=Op.AND, right=predicate)
