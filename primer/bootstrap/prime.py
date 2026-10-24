"""Prime: the default workspace.

Every rule about the default workspace lives in this module (pivot plan P2): which id it has (below), adopting an existing
install's legacy row, the caller's main operator session (per user, made on demand by the ensure route), seeding its
``agents.md`` and the reset of that file.

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

Prime's agents.md
-----------------

The platform ships ``primer/bootstrap/prime_agents.md`` (:func:`shipped_prime_agents_md`). The ensure pass writes it to the
default workspace's root as ``agents.md`` only when neither ``agents.md`` nor ``AGENTS.md`` is there
(:func:`ensure_prime_agents_md`), so a boot never overwrites a file a person edited, and never shadows an ``AGENTS.md`` (the
loader, :mod:`primer.workspace.instructions`, reads ``AGENTS.md`` only while ``agents.md`` is absent). A deleted file comes back
at the next boot, as every ensured object does; an EMPTY file is kept and adds nothing to a prompt. Only the explicit reset
(:func:`reset_prime_agents_md`: ``POST /v1/setup/reset_prime_instructions`` and the admin tool
``system__reset_prime_instructions``) rewrites an existing file.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
from typing import Any

from primer.bootstrap.defaults import (
    LEGACY_DEFAULT_WORKSPACE_ID,
    PRIME_WORKSPACE_NAME,
    RESERVED_OPERATOR_AGENT,
)
from primer.model.except_ import NotFoundError, WorkspaceUnreachableError
from primer.model.workspace import Workspace
from primer.storage import raw_generation

logger = logging.getLogger(__name__)

#: The name prime's shipped instructions are written under: the name the loader reads first (``AGENTS_MD_NAMES[0]``).
PRIME_AGENTS_MD: str = "agents.md"

#: How long, in seconds, each of the two backend steps of the seed and of the reset may take: reaching the default workspace
#: (``WorkspaceRegistry.get_workspace``, which re-attaches to its backend and on Kubernetes can wait for a Pending pod) together
#: with the check for ``agents.md`` and ``AGENTS.md``, and then the write. The ensure pass runs before the server serves, so a
#: stuck backend must not hold the boot: past either bound the seed logs one WARNING and answers ``False`` (the next boot tries
#: again), and the reset raises ``WorkspaceUnreachableError`` (503). A write given up on may still land afterwards; it writes
#: the same shipped bytes. Read when each step runs, so a test can shorten it.
PRIME_AGENTS_MD_CHECK_TIMEOUT_S: float = 10.0

#: The display name a created main session carries (task 14 finds it by this name).
MAIN_SESSION_NAME: str = "main"

#: The most open operator sessions of ONE caller on one workspace the lookup reads (one query, filtered in storage). A caller has one in
#: practice; the cap only bounds the read.
_MAIN_SESSION_PAGE: int = 200


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
      never qualify. "The caller's" is the principal's type and id and nothing else (lead ruling 4 of the G7 fixes, audit
      G7-12): a new display name, another login source (password or SSO) or a role change keeps the same session; an API
      token is its own principal. One query, filtered in storage on the workspace, ``initiated_by.id``,
      ``binding.agent_id`` and the status, reads them: never a scan of the workspace's sessions.
    * **A role change keeps the session, demotion included.** A demoted admin gets their OWN main session back: their
      steer of it is a live request on a session they started, which :func:`primer.authz.may_drive` always allows
      (``own_session``, review of #853 N1), and its runs execute at their current role, which caps the recorded admin
      rank (``resolve_run_principal``). A promotion keeps it too; it runs at the rank it was created with.
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
            row.workspace_id == workspace_id
            and row.status is not SessionStatus.ENDED
            and row.initiated_by is not None
            and (row.initiated_by.type, row.initiated_by.id) == (initiated_by.type, initiated_by.id)
            and getattr(row.binding, "agent_id", None) == RESERVED_OPERATOR_AGENT
        )

    def _eq(field: str, value: str) -> Predicate:
        return Predicate(left=FieldRef(name=field), op=Op.EQ, right=Value(value=value))

    sessions = storage_provider.get_storage(WorkspaceSession)
    predicate = _eq("workspace_id", workspace_id)
    for part in (
        _eq("initiated_by.id", initiated_by.id),
        _eq("binding.agent_id", RESERVED_OPERATOR_AGENT),
        Predicate(left=FieldRef(name="status"), op=Op.NE, right=Value(value=SessionStatus.ENDED.value)),
    ):
        predicate = Predicate(left=predicate, op=Op.AND, right=part)
    page = await sessions.find(predicate, OffsetPage(offset=0, length=_MAIN_SESSION_PAGE))
    candidates = [row for row in page.items if _is_callers_open_operator(row)]
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


def shipped_prime_agents_md() -> bytes:
    """The bytes of the instructions the platform ships for prime (``primer/bootstrap/prime_agents.md``, packaged with the code)."""
    return importlib.resources.files("primer.bootstrap").joinpath("prime_agents.md").read_bytes()


async def _has_instructions_file(workspace: Any) -> bool:
    """Whether ``agents.md`` or ``AGENTS.md`` is at the workspace's root, as an entry of any kind.

    Only :class:`NotFoundError` means absent. Any other failure propagates: a name that cannot be checked may be there, and
    writing ``agents.md`` beside an ``AGENTS.md`` would shadow it.
    """
    from primer.workspace.instructions import AGENTS_MD_NAMES

    for name in AGENTS_MD_NAMES:
        try:
            await workspace.file_info(name)
        except NotFoundError:
            continue
        return True
    return False


async def ensure_prime_agents_md(storage_provider: Any, *, workspace_registry: Any) -> bool:
    """Write the shipped instructions to the default workspace as ``agents.md`` when it has neither instructions file.

    Returns ``True`` only when this call wrote the file. ``False``, with nothing written, when there is no registry or no default
    workspace yet, when the default workspace's row is missing (the default-workspace step deferred or failed; the pass that
    creates the row seeds the file), when the backend cannot find the workspace, and when ``agents.md`` or ``AGENTS.md`` is
    already there, whatever it holds (an edited file, an empty one, a directory): the boot never overwrites or shadows what a
    person put there. Only :func:`reset_prime_agents_md` rewrites an existing file.

    Reaching the workspace and the check share one bound, :data:`PRIME_AGENTS_MD_CHECK_TIMEOUT_S`, and the write has its own of the
    same length, so a stuck backend cannot hold the boot: past either bound this logs one WARNING (no traceback) and answers
    ``False``, nothing more is written by this boot, and the next boot tries again. A write given up on may still land afterwards;
    it writes the same shipped bytes. A check that times out never reads as "absent".

    Any other failure propagates, so the ensure pass records it under its step and the next boot tries again: a check that
    fails never reads as "absent".

    Two processes seeding at once both see no file and write the same bytes, so the result is the same file. A person writing
    the file between the check and the write is the one window this leaves; the pass runs at boot and on the setup seed only.
    """
    if workspace_registry is None:
        return False
    workspace_id = await resolve_default_workspace_id(storage_provider)
    if workspace_id is None:
        return False
    if await storage_provider.get_storage(Workspace).get(workspace_id) is None:
        return False
    timeout = PRIME_AGENTS_MD_CHECK_TIMEOUT_S
    try:
        async with asyncio.timeout(timeout) as bound:
            workspace = await workspace_registry.get_workspace(workspace_id)
            present = await _has_instructions_file(workspace)
    except NotFoundError as exc:
        logger.warning(
            "prime: the default workspace could not be reached; its agents.md is seeded at a later boot: %s", exc,
            extra={"workspace_id": workspace_id},
        )
        return False
    except TimeoutError:
        if not bound.expired():
            raise
        logger.warning(
            "prime: reaching the default workspace %r and checking it for agents.md or AGENTS.md took longer than %g seconds; "
            "nothing was written, the next boot tries again", workspace_id, timeout,
        )
        return False
    if present:
        return False
    try:
        async with asyncio.timeout(timeout) as bound:
            await workspace.write_file(PRIME_AGENTS_MD, shipped_prime_agents_md())
    except TimeoutError:
        if not bound.expired():
            raise
        logger.warning(
            "prime: writing agents.md to the default workspace %r took longer than %g seconds; the write may still land (with "
            "the same shipped bytes), and the next boot checks again", workspace_id, timeout,
        )
        return False
    logger.info("prime: wrote the shipped agents.md to the default workspace", extra={"workspace_id": workspace_id})
    return True


async def _upper_case_beside(workspace: Any) -> str | None:
    """``"AGENTS.md"`` when an entry of that name is at the workspace's root, else ``None``.

    An entry the backend refuses to describe (a link out of the root, a socket: ``BadRequestError``) is there too. Any other failure
    propagates, so a reset that cannot tell writes nothing.
    """
    from primer.model.except_ import BadRequestError
    from primer.workspace.instructions import AGENTS_MD_NAMES

    name = AGENTS_MD_NAMES[1]
    try:
        await workspace.file_info(name)
    except NotFoundError:
        return None
    except BadRequestError:
        return name
    return name


async def reset_prime_agents_md(storage_provider: Any, *, workspace_registry: Any) -> dict[str, object]:
    """Overwrite ``agents.md`` in the default workspace with the shipped instructions: the one path that rewrites an existing file.

    Behind ``POST /v1/setup/reset_prime_instructions`` and the admin tool ``system__reset_prime_instructions``; who may call it is
    their concern (both are admin-only, and the tool refuses a session outside the default workspace). The bytes are always
    :func:`shipped_prime_agents_md`, never a caller's, and only ``agents.md`` is written: an ``AGENTS.md`` beside it stays where it is
    (the loader reads it only while ``agents.md`` is absent).

    Reaching the workspace and the write are each bounded by :data:`PRIME_AGENTS_MD_CHECK_TIMEOUT_S`; past either bound this raises
    :class:`WorkspaceUnreachableError` (503), and a write given up on may still land afterwards with the same shipped bytes.

    Returns ``{"workspace_id", "path", "bytes", "shadows"}``. ``shadows`` is ``"AGENTS.md"`` when an ``AGENTS.md`` is beside the
    written ``agents.md`` (the loader no longer reads it), else ``None``; it is checked with reaching the workspace, before the write.
    Raises :class:`NotFoundError` ``"no default workspace is recorded"`` when there is
    no default workspace, :class:`WorkspaceUnreachableError` on its own timeouts (above), and otherwise whatever the registry or the
    backend raises: ``NotFoundError`` for a missing row or a backend that cannot re-attach it, ``BadRequestError`` for a directory
    called ``agents.md``, ``WorkspaceRefusedError`` for a workspace the deployment refuses, and any other exception as it comes (a
    local workspace raises ``RuntimeError`` for a symlink loop at ``agents.md``), which REST answers 500.
    """
    workspace_id = await resolve_default_workspace_id(storage_provider)
    if workspace_id is None:
        raise NotFoundError("no default workspace is recorded")
    timeout = PRIME_AGENTS_MD_CHECK_TIMEOUT_S
    try:
        async with asyncio.timeout(timeout) as bound:
            workspace = await workspace_registry.get_workspace(workspace_id)
            shadows = await _upper_case_beside(workspace)
    except TimeoutError:
        if not bound.expired():
            raise
        raise WorkspaceUnreachableError(
            f"the default workspace {workspace_id!r} did not answer within {timeout:g} seconds; nothing was written"
        ) from None
    shipped = shipped_prime_agents_md()
    try:
        async with asyncio.timeout(timeout) as bound:
            await workspace.write_file(PRIME_AGENTS_MD, shipped)
    except TimeoutError:
        if not bound.expired():
            raise
        raise WorkspaceUnreachableError(
            f"writing agents.md to the default workspace {workspace_id!r} did not finish within {timeout:g} seconds; the write "
            "may still land (with the same shipped bytes)"
        ) from None
    logger.info("prime: reset agents.md to the shipped version", extra={"workspace_id": workspace_id})
    return {"workspace_id": workspace_id, "path": PRIME_AGENTS_MD, "bytes": len(shipped), "shadows": shadows}


__all__ = [
    "MAIN_SESSION_NAME",
    "PRIME_AGENTS_MD",
    "PRIME_AGENTS_MD_CHECK_TIMEOUT_S",
    "adopt_legacy_default_workspace",
    "ensure_main_operator_session",
    "ensure_prime_agents_md",
    "main_session_id",
    "record_default_workspace_id",
    "reset_prime_agents_md",
    "resolve_default_workspace_id",
    "shipped_prime_agents_md",
]
