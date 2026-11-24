"""Reserve a child ID before external I/O and decide its publication exactly once."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from primer.access.person import Person
from primer.access.policy import require
from primer.int.storage_provider import StorageProvider
from primer.model.access import AccessLevel
from primer.model.agent import Agent
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.graph import Graph
from primer.model.session_creation import SessionCreationIntent
from primer.model.storage import OffsetPage, Op, OrderBy
from primer.model.workspace import Workspace
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.storage.q import Q
from primer.workspace.work_priority import choose_creation_priority

if TYPE_CHECKING:
    from primer.trigger.subscribers import TriggerFireMetadata

logger = logging.getLogger(__name__)
# Recovery may decline a slow allocation after this grace. It never touches its files.
PREPARATION_GRACE = timedelta(hours=1)


def _caller_version(row: WorkspaceSession | None, turn_no: int | None):
    if (
        row is None
        or row.status in (SessionStatus.CREATED, SessionStatus.ENDED, SessionStatus.PAUSED)
        or row.cancel_requested or row.interrupt_requested or row.pause_requested
        or row.completed_turn_no == row.turn_no
        or (turn_no is not None and row.turn_no != turn_no)
    ):
        raise ConflictError("calling session is no longer active for this operation")
    return (
        row.workspace_id, row.created_at, row.responder_epoch, row.binding_epoch,
        row.turn_no, row.status, row.turn_status, row.parked_status, row.parked_at,
        row.turn_started_at, row.metadata.get("invocation"), row.priority,
    )


@dataclass(frozen=True)
class CreationAttempt:
    receipt: SessionCreationIntent
    workspace: Workspace
    person: Person | None
    sources: tuple[Agent | Graph, ...]
    caller_id: str | None
    caller_turn: int | None
    caller_version: tuple | None

    def check_workspace(self, current: Workspace | None) -> None:
        before = self.workspace
        if (
            current is None or current.phase == "terminating"
            or current.created_at != before.created_at
            or current.provider_id != before.provider_id
            or current.runtime_meta != before.runtime_meta
        ):
            raise NotFoundError(f"Workspace {before.id!r} does not exist")
        if self.person is not None:
            require(self.person, current.access, AccessLevel.FULL, what=f"Workspace {before.id!r}")

    def check_caller(self, row: WorkspaceSession | None) -> None:
        if _caller_version(row, self.caller_turn) != self.caller_version:
            raise ConflictError("calling session changed during child creation")

    async def publish(self, sp: StorageProvider, session: WorkspaceSession) -> None:
        if session.id != self.receipt.id or session.workspace_id != self.receipt.workspace_id:
            raise ConflictError("session does not match its creation receipt")
        sessions = sp.get_storage(WorkspaceSession)
        workspaces = sp.get_storage(Workspace)
        models = (SessionCreationIntent, WorkspaceSession, Workspace, *dict.fromkeys(type(s) for s in self.sources))
        async with sp.locking_transaction(models=models) as conn:
            # Binding and any graph external-tool permission witness precede the
            # receipt in table order. A changed snapshot requires a fresh start.
            for source in self.sources:
                current = await sp.get_storage(type(source)).get_for_update(source.id, conn=conn)
                if current is None or current != source:
                    raise ConflictError("session binding changed during slot preparation")

            self.check_workspace(await workspaces.get(session.workspace_id, conn=conn))
            if self.caller_id is not None:
                self.check_caller(await sessions.get(self.caller_id, conn=conn))

            receipt = await sp.get_storage(SessionCreationIntent).patch_if(
                session.id, {"state": "committed"},
                where={
                    "state": ["preparing"], "occurrence": [self.receipt.occurrence],
                    "workspace_id": [session.workspace_id], "priority": [session.priority],
                },
                conn=conn,
            )
            if receipt is None:
                raise ConflictError("session creation was already decided")

            # SQLite has held its writer reservation since BEGIN. On Postgres the
            # prechecks above are snapshots; lock/recheck through commit. Insert
            # the fresh unique child at its ordered position among the session IDs.
            if self.caller_id is not None and self.caller_id < session.id:
                self.check_caller(await sessions.get_for_update(self.caller_id, conn=conn))
            await sessions.create(session, conn=conn)
            if self.caller_id is not None and self.caller_id > session.id:
                self.check_caller(await sessions.get_for_update(self.caller_id, conn=conn))
            self.check_workspace(await workspaces.get_for_update(session.workspace_id, conn=conn))


@asynccontextmanager
async def preparing_creation(
    sp: StorageProvider, *, sid: str, workspace: Workspace, person: Person | None,
    sources: Sequence[Agent | Graph], caller_id: str | None, caller_turn: int | None,
    requested_priority: int | None, fire: TriggerFireMetadata | None, binding_default: int,
    nonzero_priority_available: bool,
) -> AsyncIterator[CreationAttempt]:
    """Only the allocator that inserted the receipt may attempt to publish."""
    sessions = sp.get_storage(WorkspaceSession)
    if caller_id == sid:
        raise ConflictError("child ID collides with calling session")
    ordered_sources = sorted(sources, key=lambda source: (type(source).__name__, source.id))
    witnesses = tuple(source.model_copy(deep=True) for source in ordered_sources)
    caller = await sessions.get(caller_id) if caller_id is not None else None
    version = _caller_version(caller, caller_turn) if caller_id is not None else None
    priority = choose_creation_priority(
        requested=requested_priority, caller=caller, fire=fire, binding_default=binding_default,
        nonzero_available=nonzero_priority_available,
    )
    receipt = SessionCreationIntent(id=sid, workspace_id=workspace.id, priority=priority)

    attempt = CreationAttempt(
        receipt=receipt, workspace=workspace.model_copy(deep=True), person=person,
        sources=witnesses,
        caller_id=caller_id, caller_turn=caller_turn, caller_version=version,
    )
    attempt.check_workspace(workspace)
    async with sp.locking_transaction(models=(SessionCreationIntent, WorkspaceSession)) as conn:
        await sp.get_storage(SessionCreationIntent).create(receipt, conn=conn)
        if await sessions.get_for_update(sid, conn=conn) is not None:
            raise ConflictError("child ID is already in use")
    try:
        yield attempt
    except BaseException:
        # A lost COMMIT response may mean publication succeeded. The guarded
        # decision serializes with that transaction and can never undo it.
        try:
            await abandon_preparation(sp, receipt)
        except Exception:
            logger.exception("session creation %s needs reconciliation", sid)
        raise


async def abandon_preparation(
    sp: StorageProvider, receipt: SessionCreationIntent,
) -> SessionCreationIntent | None:
    return await sp.get_storage(SessionCreationIntent).patch_if(
        receipt.id, {"state": "abandoned"},
        where={"state": ["preparing"], "occurrence": [receipt.occurrence]},
    )


async def recover_preparations(sp: StorageProvider, *, now: datetime | None = None) -> None:
    """Fence old unconfirmed attempts. Neither state nor age authorizes slot deletion."""
    cutoff = (now or datetime.now(UTC)) - PREPARATION_GRACE
    rows = sp.get_storage(SessionCreationIntent)
    # Seek by ID rather than offset: abandoning rows removes them from the
    # predicate. A new lower ID inserted mid-scan is visited on the next tick.
    last = None
    while True:
        query = Q(SessionCreationIntent).where("state", "preparing")
        if last is not None:
            query.where("id", last, Op.GT)
        page = await rows.find(
            query.build(), OffsetPage(length=100, want_total=False), order_by=[OrderBy(field="id")],
        )
        for receipt in page.items:
            if receipt.created_at >= cutoff:
                continue
            try:
                if await abandon_preparation(sp, receipt) is not None:
                    logger.warning("session creation %s abandoned; retaining any workspace slot", receipt.id)
            except Exception:
                logger.exception("cannot reconcile session creation %s", receipt.id)
        if len(page.items) < 100:
            return
        last = page.items[-1].id
