"""Change only the priority of the park a driver actually observed."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from primer.access.person import Person, person_from_ref
from primer.access.policy import require
from primer.model.access import AccessLevel
from primer.model.api_token import ApiToken
from primer.model.except_ import AccessDeniedError, ConflictError, NotFoundError
from primer.model.user import User
from primer.model.work_priority import validate_work_priority
from primer.model.workspace import Workspace
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.yields import _datetime_fence_spellings
from primer.workspace.session_creation import _caller_version

if TYPE_CHECKING:
    from primer.workspace.session_factory import SessionFactoryDeps


def parked_priority_token(row: WorkspaceSession) -> str | None:
    """A read precondition only. Authorization is evaluated again under lock."""
    if (
        row.parked_status != "parked" or row.parked_at is None
        or row.status == SessionStatus.ENDED or row.cancel_requested or row.interrupt_requested or row.pause_requested
        or row.turn_status == "arming" or row.pending_responder_intent is not None
    ):
        return None
    generation = (
        row.id, row.workspace_id, row.created_at.isoformat(), row.parked_at.isoformat(),
        row.status.value, row.turn_no, row.turn_status, row.binding_epoch, row.responder_epoch,
        row.responder_seq, row.responder_reservation_version, row.turn_person_seq, row.metadata.get("invocation"),
        row.parked_event_key, row.parked_event_keys, row.priority, row.priority_revision,
    )
    return hashlib.sha256(json.dumps(generation, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _calling_turn(row: WorkspaceSession | None, turn: int):
    lifecycle = _caller_version(row, turn)
    assert row is not None  # _caller_version refuses a missing calling session
    # The creation lifecycle already includes priority. Its revision also
    # distinguishes a parked edit followed by a return to the original value.
    return lifecycle, row.priority_revision, row.turn_person or row.initiated_by, row.turn_person_seq


def _require_turn_authority(caller: WorkspaceSession, person: Person, priority: int) -> None:
    # A durable initiator caps a turn's authority. The request may have
    # been re-ranked after a demotion but cannot inherit a promotion.
    starter = person_from_ref(caller.turn_person or caller.initiated_by)
    if (
        starter.user_id != person.user_id or (starter.role == "system") != (person.role == "system")
        or (person.is_admin and not starter.is_admin) or (person.is_full_user and not starter.is_full_user)
    ):
        raise AccessDeniedError("calling turn's acting person changed")
    if priority < caller.priority:
        raise AccessDeniedError("priority cannot be more urgent than the calling session")


async def set_parked_priority(
    deps: SessionFactoryDeps, *, workspace_id: str, session_id: str, generation: str,
    priority: int, person: Person, caller_id: str | None, caller_turn: int | None,
    token_id: str | None = None, account_epoch: int | None = None,
) -> WorkspaceSession:
    priority = validate_work_priority(priority)
    sp = deps.storage_provider
    sessions = sp.get_storage(WorkspaceSession)
    token_ids = {token_id} if token_id is not None else set()
    # Capture the caller before waiting for locks. A replacement or changed
    # turn cannot lend its identity to the original in-flight invocation.
    if caller_id is not None:
        if caller_turn is None:
            raise ConflictError("calling session turn is required for a priority edit")
        observed = await sessions.get(caller_id)
        caller_version = _calling_turn(observed, caller_turn)
        starter = observed.turn_person or observed.initiated_by
        if starter is not None and starter.type == "api_token":
            token_ids.add(starter.id)

    # Token, session, user, workspace is storage table order. Locks survive
    # the final guarded write, including grants in a distinct caller workspace.
    async with sp.locking_transaction(models=(ApiToken, WorkspaceSession, User, Workspace)) as conn:
        tokens = []
        for tid in sorted(token_ids):
            token = await sp.get_storage(ApiToken).get_for_update(tid, conn=conn)
            if token is None or token.revoked_at is not None or token.user_id != person.user_id:
                raise AccessDeniedError("the acting token is no longer authorized")
            tokens.append(token)
        ids = {session_id} if caller_id is None else {session_id, caller_id}
        locked = {}
        for sid in sorted(ids):
            locked[sid] = await sessions.get_for_update(sid, conn=conn)
        target = locked[session_id]
        missing = f"Session {session_id!r} does not exist"
        if target is None or target.workspace_id != workspace_id:
            raise NotFoundError(missing)
        if person.user_id is not None:
            user = await sp.get_storage(User).get_for_update(person.user_id, conn=conn)
            if (user is None or user.disabled or user.role != person.role
                    or (account_epoch is not None and user.session_epoch != account_epoch)):
                raise AccessDeniedError("the acting account no longer has its original authority")
        workspace_ids = {workspace_id}
        if caller_id is not None:
            workspace_ids.add(observed.workspace_id)
        for wid in sorted(workspace_ids):
            workspace = await sp.get_storage(Workspace).get_for_update(wid, conn=conn)
            if workspace is None or workspace.phase == "terminating":
                raise NotFoundError(missing)
            require(person, workspace.access, AccessLevel.FULL, what=f"Session {session_id!r}")
        for token in tokens:
            expires = token.expires_at
            if expires is not None and expires.replace(tzinfo=expires.tzinfo or UTC) <= datetime.now(UTC):
                raise AccessDeniedError("the acting token expired")
        if caller_id is not None:
            caller = locked[caller_id]
            if _calling_turn(caller, caller_turn) != caller_version:
                raise ConflictError("calling session changed during priority edit")
            _require_turn_authority(caller, person, priority)
        current_generation = parked_priority_token(target)
        if current_generation is None or current_generation != generation:
            raise ConflictError("park changed; read the session again before editing priority")
        if priority == target.priority:
            return target
        result = await sessions.patch_if(
            session_id, {"priority": priority, "priority_revision": target.priority_revision + 1},
            where={"parked_at": _datetime_fence_spellings(target, "parked_at"), "parked_status": ["parked"],
                   "priority_revision": [target.priority_revision]}, conn=conn,
        )
        if result is None:
            raise ConflictError("park changed; read the session again before editing priority")
        return result
