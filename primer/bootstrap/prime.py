"""Prime: the default workspace.

Every rule about the default workspace lives in this module (pivot plan P2): which id it has (below), adopting an existing
install's legacy row, the caller's main operator session (per user, made on demand by the ensure route), and, in later
tasks, seeding its ``agents.md`` and the reset.

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

The main operator session
--------------------------

The default workspace's main operator session is NOT seeded at boot: each user has their own, made on demand by
``POST /v1/workspaces/{id}/main_session`` (:func:`ensure_main_operator_session`) and created as the caller, so it runs at
exactly the caller's rank. The id is :func:`main_session_id`.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.bootstrap.defaults import (
    LEGACY_DEFAULT_WORKSPACE_ID,
    PRIME_WORKSPACE_NAME,
    RESERVED_OPERATOR_AGENT,
)
from primer.model.except_ import NotFoundError
from primer.model.workspace import Workspace
from primer.storage import raw_generation

logger = logging.getLogger(__name__)

#: The display name a created main session carries (task 14 finds it by this name).
MAIN_SESSION_NAME: str = "main"

#: The console's own session-list cap; the lookup pages at this size and stops at a short page.
_MAIN_SESSION_PAGE: int = 200
#: A workspace with more sessions than this is beyond anything the console lists: give up the lookup and make the main
#: session rather than scan the whole table.
_MAIN_SESSION_MAX_PAGES: int = 50


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


def main_session_id(workspace_id: str, n: int) -> str:
    """The deterministic id of the n-th main operator session on the workspace.

    ``sess-<workspace id>-main-<n>``: it never collides with a route-made ``sess-<12 hex>`` id, and two concurrent
    callers pick the same id for the same slot, so at most one of them wins the create; the other's ``ConflictError`` is
    the signal that the slot is taken.
    """
    return f"sess-{workspace_id}-main-{n}"


async def ensure_main_operator_session(
    storage_provider: Any,
    *,
    workspace_id: str,
    initiated_by: "PrincipalRef",
    session_deps: "SessionFactoryDeps | None" = None,
) -> tuple["WorkspaceSession", bool]:
    """The caller's own main operator session on the workspace, created as the caller when they have none.

    Returns ``(row, created)``; ``created`` is True only when THIS call made the row.

    * **The lookup is the caller's own.** The caller's open (status not ``ended``) sessions on the workspace bound to
      the ``operator`` agent; when more than one is open, the most recently active wins, so a person lands in the
      conversation they were in, never a fresh one. Another user's session, a non-operator binding and an ended session
      never qualify.
    * **A created session is named ``main`` and runs as the caller.** ``initiated_by`` carries the caller's role, so the
      session clears exactly the caller's role floor (A-20): an admin's main session can call the admin tools through
      the operator, a user's cannot. It is made exactly as the workspace-create seed makes its ``main`` (the plain
      ``create_session`` with ``auto_start=False``; the on-disk slot arrives when the session first runs).
    * **One main session under concurrency.** The id is :func:`main_session_id` with the smallest free ``n`` and the
      create is the claim: a concurrent caller that picks the same id loses the insert and re-reads the row; it returns
      the row only when it is this caller's open operator session, and takes the next free ``n`` otherwise (an ended
      main or another user's main does not belong to this caller).
    """
    from primer.model.agent import Agent
    from primer.model.except_ import ConflictError
    from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )
    from primer.workspace.session_factory import (
        SessionFactoryDeps,
        create_session,
    )

    def _is_callers_open_operator(row: WorkspaceSession) -> bool:
        return (
            row.status is not SessionStatus.ENDED
            and row.initiated_by is not None
            and row.initiated_by == initiated_by
            and getattr(row.binding, "agent_id", None) == RESERVED_OPERATOR_AGENT
        )

    sessions = storage_provider.get_storage(WorkspaceSession)
    predicate = Predicate(
        left=FieldRef(name="workspace_id"), op=Op.EQ, right=Value(value=workspace_id),
    )
    candidates: list[WorkspaceSession] = []
    offset = 0
    for _ in range(_MAIN_SESSION_MAX_PAGES):
        page = await sessions.find(
            predicate, OffsetPage(offset=offset, length=_MAIN_SESSION_PAGE),
        )
        candidates.extend(row for row in page.items if _is_callers_open_operator(row))
        if len(page.items) < _MAIN_SESSION_PAGE:
            break
        offset += _MAIN_SESSION_PAGE
    if candidates:
        newest = max(
            candidates, key=lambda row: (row.last_turn_at or row.created_at, row.created_at),
        )
        return newest, False

    if await storage_provider.get_storage(Agent).get(RESERVED_OPERATOR_AGENT) is None:
        raise NotFoundError(f"Agent {RESERVED_OPERATOR_AGENT!r} does not exist")

    deps = session_deps or SessionFactoryDeps(
        storage_provider=storage_provider, claim_engine=None, scheduler=None, workspace_registry=None,
    )
    n = 1
    while True:
        candidate_id = main_session_id(workspace_id, n)
        try:
            row = await create_session(
                workspace_id=workspace_id,
                binding=AgentSessionBinding(agent_id=RESERVED_OPERATOR_AGENT),
                initial_instructions=None,
                graph_input=None,
                auto_start=False,
                metadata=None,
                name=MAIN_SESSION_NAME,
                session_id=candidate_id,
                initiated_by=initiated_by,
                deps=deps,
            )
            logger.info(
                "prime: created the caller's main operator session",
                extra={"session_id": candidate_id, "workspace_id": workspace_id},
            )
            return row, True
        except ConflictError:
            existing = await sessions.get(candidate_id)
            if existing is not None and _is_callers_open_operator(existing):
                return existing, False
            n += 1


__all__ = [
    "MAIN_SESSION_NAME",
    "adopt_legacy_default_workspace",
    "ensure_main_operator_session",
    "main_session_id",
    "record_default_workspace_id",
    "resolve_default_workspace_id",
]
