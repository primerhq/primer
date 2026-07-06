"""Workspace REST surface — providers, templates, workspaces + sub-resources.

Three entity routers and three sub-resources on Workspace:

* ``WorkspaceProvider`` — list / get / create / update / delete. Reserved
  bootstrap-managed providers (see
  :data:`~primer.api.registries.provider_registry.RESERVED_WORKSPACE_PROVIDER_IDS`)
  are read-only: PUT and DELETE against a reserved id return 403.
* ``WorkspaceTemplate`` — full CRUD (list / get / create / update /
  delete).
* ``Workspace`` — list / get / create / delete (no update). Body of
  ``POST`` is :class:`WorkspaceCreateBody` (template id + optional
  overrides).

Sub-resources on ``/v1/workspaces/{id}``:

* Sessions — list, get, pause, resume, steer.
* Files — list (paginated ls), info, read, download, delete, write.
* Log — git log over the ``.state`` repo.
* Yields — aggregated pending yields across all sessions (Studio A3).
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_claim_engine,
    get_event_bus,
    get_scheduler,
    get_session_storage,
    get_storage_provider,
    get_workspace_provider_storage,
    get_workspace_registry,
    get_workspace_storage,
    get_workspace_template_storage,
)
from primer.api.errors import PROBLEM_JSON_MEDIA_TYPE, common_responses
from primer.model.problem_details import ProblemDetails
from primer.api.pagination import FindRequest, parse_order_by, parse_page
from primer.api.registries import WorkspaceRegistry
from primer.api.registries.provider_registry import RESERVED_WORKSPACE_PROVIDER_IDS
from primer.api.routers._crud import make_crud_router
from primer.bootstrap.defaults import RESERVED_WORKSPACE_TEMPLATES
from primer.model.except_ import (
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from primer.model.storage import (
    CursorPageResponse,
    OffsetPageResponse,
    OrderBy,
    PageRequest,
)
from primer.model.workspace import (
    FileEntry,
    Workspace as WorkspaceRow,
    WorkspaceChannelLink,
    WorkspaceDiagnosticResult,
    WorkspaceProvider,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.mutation_lock import session_lifecycle_lock


logger = logging.getLogger(__name__)


# ===========================================================================
# Request / response bodies
# ===========================================================================


class WorkspaceCreateBody(BaseModel):
    """Body of ``POST /v1/workspaces``."""

    id: str | None = Field(
        default=None,
        description=(
            "Optional caller-supplied workspace id. If omitted, the "
            "backend allocates one."
        ),
    )
    name: str | None = Field(
        default=None,
        description=(
            "Optional human-readable label shown in the console in place "
            "of the id. Does not affect the workspace id or any handle."
        ),
    )
    template_id: str = Field(
        ...,
        min_length=1,
        description="Id of the WorkspaceTemplate to materialise.",
    )
    overrides: WorkspaceTemplateOverrides | None = Field(
        default=None,
        description=(
            "Optional per-instantiation overrides (env additions, "
            "extra files, additional init commands)."
        ),
    )
    reply_binding: WorkspaceChannelLink | None = Field(
        default=None,
        description=(
            "Optional reply binding to set at create time. "
            "When set, the workspace row is created with this "
            "reply_binding already populated."
        ),
    )


class FileWriteBody(BaseModel):
    """Body of ``PUT /v1/workspaces/{id}/files``."""

    content: str = Field(
        ...,
        description=(
            "File content. Decoded according to ``encoding``. Empty "
            "string is permitted — it produces an empty file."
        ),
    )
    encoding: Literal["text", "base64"] = Field(
        default="text",
        description=(
            "How to interpret ``content``. ``text`` is UTF-8 encoded "
            "as-is; ``base64`` is decoded to raw bytes."
        ),
    )


class FileReadResponse(BaseModel):
    """Body returned by ``GET /v1/workspaces/{id}/files/read``."""

    path: str
    encoding: Literal["text", "base64"]
    content: str
    size_bytes: int
    mtime: float | None = None
    mtime_iso: str | None = None
    etag: str | None = None


class DiagnosticExecBody(BaseModel):
    """Body of ``POST /v1/workspaces/{id}/diagnostic``."""

    command: str = Field(
        ...,
        min_length=1,
        description=(
            "Shell command to run. Must start with one of the "
            "whitelisted command names (``echo``, ``pwd``, ``whoami``, "
            "``uname``, ``ls``) — anything else is rejected with 400. "
            "This is a read-only diagnostic surface, not arbitrary RCE."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0.0,
        le=30.0,
        description=(
            "Per-call timeout ceiling. Defaults to 5.0 if omitted. "
            "Hard-capped at 30s — the route is for liveness smokes, "
            "not long-running jobs."
        ),
    )


_DIAGNOSTIC_COMMAND_WHITELIST: frozenset[str] = frozenset(
    {"echo", "pwd", "whoami", "uname", "ls"}
)


class SteerBody(BaseModel):
    """Body of ``POST /v1/workspaces/{id}/sessions/{sid}/steer``."""

    instruction: str = Field(
        ...,
        min_length=1,
        description=(
            "User-role text appended as a fresh ``user_instruction`` "
            "message in the session's transcript."
        ),
    )


class SessionRenameBody(BaseModel):
    """Body of ``PATCH /v1/workspaces/{id}/sessions/{sid}``."""

    name: str | None = Field(
        default=None,
        description=(
            "New friendly name for the session. Pass null or an empty / "
            "whitespace-only string to clear it and fall back to the id in "
            "the console."
        ),
    )


# ===========================================================================
# Provider router (CRUD minus update)
# ===========================================================================


async def _invalidate_workspace_backend(
    entity_id: str, request: Request
) -> None:
    registry: WorkspaceRegistry = request.app.state.workspace_registry
    await registry.invalidate(entity_id)


async def _reject_reserved_workspace_provider_create(
    entity, request: Request
) -> None:
    """Reject POST /v1/workspace_providers with a reserved id (409)."""
    if entity.id in RESERVED_WORKSPACE_PROVIDER_IDS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "reserved_id",
                "kind": "workspace_provider",
                "reserved": sorted(RESERVED_WORKSPACE_PROVIDER_IDS),
                "message": (
                    f"id {entity.id!r} is reserved and cannot be "
                    "created via the API"
                ),
            },
        )


async def _reject_reserved_workspace_provider_delete(
    entity_id: str, request: Request
) -> None:
    """Reject DELETE /v1/workspace_providers/<reserved-id> (403)."""
    if entity_id in RESERVED_WORKSPACE_PROVIDER_IDS:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_id_protected",
                "kind": "workspace_provider",
                "message": (
                    f"id {entity_id!r} is a reserved workspace provider "
                    "and cannot be deleted"
                ),
            },
        )


async def _reject_reserved_workspace_provider_update(
    entity, existing, request: Request
) -> None:
    """Reject PUT /v1/workspace_providers/<reserved-id> (403).

    Reserved providers (see ``RESERVED_WORKSPACE_PROVIDER_IDS``) are
    auto-recreated from config on boot; mutating them via the API would
    desync the runtime state from the bootstrap defaults.
    """
    if existing is not None and existing.id in RESERVED_WORKSPACE_PROVIDER_IDS:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_id_protected",
                "kind": "workspace_provider",
                "message": (
                    f"id {existing.id!r} is a reserved workspace provider "
                    "and cannot be updated"
                ),
            },
        )


provider_router = make_crud_router(
    model_cls=WorkspaceProvider,
    storage_dep=get_workspace_provider_storage,
    plural="workspace_providers",
    tag="workspace-providers",
    on_delete=_invalidate_workspace_backend,
    on_pre_create=_reject_reserved_workspace_provider_create,
    on_pre_update=_reject_reserved_workspace_provider_update,
    on_pre_delete_id=_reject_reserved_workspace_provider_delete,
)


# ===========================================================================
# Template router (full CRUD)
# ===========================================================================

# Reserved template ids — bootstrapped by BootstrapRunner on first boot
# and protected against API mutation/deletion to keep runtime state in
# sync with the bootstrap defaults.
RESERVED_WORKSPACE_TEMPLATE_IDS: frozenset[str] = frozenset(
    RESERVED_WORKSPACE_TEMPLATES.keys()
)


async def _reject_reserved_workspace_template_create(
    entity, request: Request
) -> None:
    """Reject POST /v1/workspace_templates with a reserved id (409)."""
    if entity.id in RESERVED_WORKSPACE_TEMPLATE_IDS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "reserved_id",
                "kind": "workspace_template",
                "reserved": sorted(RESERVED_WORKSPACE_TEMPLATE_IDS),
                "message": (
                    f"id {entity.id!r} is reserved and cannot be "
                    "created via the API"
                ),
            },
        )


async def _reject_reserved_workspace_template_delete(
    entity_id: str, request: Request
) -> None:
    """Reject DELETE /v1/workspace_templates/<reserved-id> (403)."""
    if entity_id in RESERVED_WORKSPACE_TEMPLATE_IDS:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_id_protected",
                "kind": "workspace_template",
                "message": (
                    f"id {entity_id!r} is a reserved workspace template "
                    "and cannot be deleted"
                ),
            },
        )


async def _reject_reserved_workspace_template_update(
    entity, existing, request: Request
) -> None:
    """Reject PUT /v1/workspace_templates/<reserved-id> (403).

    Reserved templates (see ``RESERVED_WORKSPACE_TEMPLATE_IDS``) are
    auto-recreated from config on boot; mutating them via the API would
    desync the runtime state from the bootstrap defaults.
    """
    if existing is not None and existing.id in RESERVED_WORKSPACE_TEMPLATE_IDS:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_id_protected",
                "kind": "workspace_template",
                "message": (
                    f"id {existing.id!r} is a reserved workspace template "
                    "and cannot be updated"
                ),
            },
        )


template_router = make_crud_router(
    model_cls=WorkspaceTemplate,
    storage_dep=get_workspace_template_storage,
    plural="workspace_templates",
    tag="workspace-templates",
    on_pre_create=_reject_reserved_workspace_template_create,
    on_pre_update=_reject_reserved_workspace_template_update,
    on_pre_delete_id=_reject_reserved_workspace_template_delete,
)


# ===========================================================================
# Workspace router (CRUD minus update; create + delete are bespoke)
# ===========================================================================

workspace_router = APIRouter(tags=["workspaces"])

_PageResp = OffsetPageResponse[Any] | CursorPageResponse[Any]


@workspace_router.get(
    "/workspaces",
    summary="List Workspaces",
    responses=common_responses(400, 422, 500),
)
async def list_workspaces(
    page: PageRequest = Depends(parse_page),
    order_by: list[OrderBy] | None = Depends(parse_order_by),
    storage=Depends(get_workspace_storage),
) -> _PageResp:
    return await storage.list(page, order_by=order_by)


@workspace_router.post(
    "/workspaces/find",
    summary="Find Workspaces with predicate",
    responses=common_responses(400, 422, 500),
)
async def find_workspaces(
    body: FindRequest,
    storage=Depends(get_workspace_storage),
) -> _PageResp:
    return await storage.find(body.predicate, body.page, order_by=body.order_by)


@workspace_router.get(
    "/workspaces/{workspace_id}",
    response_model=WorkspaceRow,
    summary="Get Workspace by id",
    responses=common_responses(404, 500),
)
async def get_workspace(
    workspace_id: str = Path(..., description="Workspace id"),
    storage=Depends(get_workspace_storage),
) -> WorkspaceRow:
    row = await storage.get(workspace_id)
    if row is None:
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
    return row


@workspace_router.post(
    "/workspaces",
    response_model=WorkspaceRow,
    status_code=201,
    summary="Create Workspace from template",
    responses=common_responses(404, 409, 422, 500),
)
async def create_workspace(
    body: WorkspaceCreateBody,
    workspace_storage=Depends(get_workspace_storage),
    template_storage=Depends(get_workspace_template_storage),
    provider_storage=Depends(get_workspace_provider_storage),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> WorkspaceRow:
    template = await template_storage.get(body.template_id)
    if template is None:
        raise NotFoundError(
            f"WorkspaceTemplate {body.template_id!r} does not exist"
        )
    if body.id is not None:
        existing = await workspace_storage.get(body.id)
        if existing is not None:
            raise ConflictError(
                f"Workspace with id {body.id!r} already exists"
            )

    # Reserve agent_sandbox slot — k8s provider variant=agent_sandbox is
    # accepted at provider-create time but workspace materialisation is
    # not implemented in v1 (see redesign spec §9).
    provider = await provider_storage.get(template.provider_id)
    if provider is None:
        raise NotFoundError(
            f"WorkspaceProvider {template.provider_id!r} does not exist"
        )
    if (
        provider.config.kind == "kubernetes"
        and getattr(provider.config, "variant", "system") == "agent_sandbox"
    ):
        raise HTTPException(
            status_code=501,
            detail={
                "error": "not_implemented",
                "message": (
                    "K8s provider variant=agent_sandbox is reserved "
                    "(see redesign spec §9). Implementation lands in a "
                    "follow-up engagement; switch the provider variant to "
                    "'system' to use the StatefulSet+Service path."
                ),
            },
        )

    live = await registry.materialise(template=template, overrides=body.overrides)

    row_id = body.id if body.id is not None else live.id
    # Mark the row "running" immediately — materialise() returned a live
    # handle, so the workspace IS up. The probe loop transitions from
    # running <-> failed thereafter; without this initial mark the row
    # would sit at the default "pending" forever and the probe skips it.
    row = WorkspaceRow(
        id=row_id,
        name=body.name,
        template_id=body.template_id,
        provider_id=template.provider_id,
        overrides=body.overrides,
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=live.runtime_meta,
        reply_binding=body.reply_binding,
    )
    await workspace_storage.create(row)
    return row


class WorkspaceRenameBody(BaseModel):
    """Body of ``PATCH /v1/workspaces/{id}``."""

    name: str | None = Field(
        default=None,
        description=(
            "New human-readable label. Pass null or an empty string to "
            "clear the name and fall back to the id in the console."
        ),
    )


@workspace_router.patch(
    "/workspaces/{workspace_id}",
    response_model=WorkspaceRow,
    summary="Rename a Workspace (set its human-readable label)",
    responses=common_responses(404, 422, 500),
)
async def rename_workspace(
    workspace_id: str = Path(..., description="Workspace id"),
    body: WorkspaceRenameBody = Body(...),
    storage=Depends(get_workspace_storage),
) -> WorkspaceRow:
    """Update only the workspace's human-readable name.

    Workspaces have no general update route (their contents are mutated
    through the files / sessions sub-APIs, not by re-PUTing the row).
    This focused PATCH lets operators label an existing workspace. An
    empty or null name clears the label.
    """
    row = await storage.get(workspace_id)
    if row is None:
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
    new_name = (body.name or "").strip() or None
    updated = row.model_copy(update={"name": new_name})
    await storage.update(updated)
    return updated


class _ReplyBindingBody(BaseModel):
    """Body of ``PUT /v1/workspaces/{id}/reply_binding``."""

    channel_id: str = Field(..., min_length=1, description="Channel id to bind.")


@workspace_router.put(
    "/workspaces/{workspace_id}/reply_binding",
    response_model=WorkspaceRow,
    summary="Set the reply binding for a Workspace",
    responses=common_responses(404, 409, 422, 500),
)
async def set_workspace_reply_binding(
    workspace_id: str = Path(..., description="Workspace id"),
    body: _ReplyBindingBody = Body(...),
    workspace_storage=Depends(get_workspace_storage),
    sp=Depends(get_storage_provider),
) -> WorkspaceRow:
    """Attach a Channel reply binding to this workspace.

    After this call, session gates (ask_user / tool_approval) on this
    workspace forward to the bound channel. Validates that the channel
    exists and that the workspace is not in a terminating phase.
    """
    from primer.model.channel import Channel

    row = await workspace_storage.get(workspace_id)
    if row is None:
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
    if row.phase == "terminating":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "workspace_terminating",
                "message": (
                    f"Workspace {workspace_id!r} is terminating and "
                    "cannot have its reply binding changed."
                ),
            },
        )
    channel_storage = sp.get_storage(Channel)
    channel = await channel_storage.get(body.channel_id)
    if channel is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "channel_not_found",
                "channel_id": body.channel_id,
                "message": f"Channel {body.channel_id!r} does not exist.",
            },
        )
    updated = row.model_copy(
        update={"reply_binding": WorkspaceChannelLink(channel_id=body.channel_id)}
    )
    await workspace_storage.update(updated)
    return updated


@workspace_router.delete(
    "/workspaces/{workspace_id}/reply_binding",
    status_code=204,
    summary="Clear the reply binding for a Workspace",
    responses=common_responses(404, 500),
)
async def clear_workspace_reply_binding(
    workspace_id: str = Path(..., description="Workspace id"),
    workspace_storage=Depends(get_workspace_storage),
) -> None:
    """Detach the reply binding from this workspace.

    After this call, session gates on this workspace are no longer
    forwarded to any channel. No-ops silently if the binding was
    already cleared.
    """
    row = await workspace_storage.get(workspace_id)
    if row is None:
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
    updated = row.model_copy(update={"reply_binding": None})
    await workspace_storage.update(updated)


@workspace_router.delete(
    "/workspaces/{workspace_id}",
    status_code=204,
    summary="Destroy Workspace",
    responses=common_responses(404, 500),
)
async def delete_workspace(
    workspace_id: str = Path(..., description="Workspace id"),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> None:
    await registry.destroy(workspace_id)


@workspace_router.post(
    "/workspaces/{workspace_id}/pause",
    status_code=501,
    summary="Pause a workspace (reserved — not implemented in v1)",
)
async def pause_workspace(workspace_id: str) -> dict:
    raise HTTPException(
        status_code=501,
        detail={
            "error": "not_implemented",
            "message": (
                "Workspace pause is reserved in v1 (see redesign spec §8.4). "
                "Backend implementations: container=docker stop, "
                "k8s=STS scale-to-0, local=no-op."
            ),
        },
    )


@workspace_router.post(
    "/workspaces/{workspace_id}/resume",
    status_code=501,
    summary="Resume a workspace (reserved — not implemented in v1)",
)
async def resume_workspace(workspace_id: str) -> dict:
    raise HTTPException(
        status_code=501,
        detail={
            "error": "not_implemented",
            "message": (
                "Workspace resume is reserved in v1 (see redesign spec §8.4)."
            ),
        },
    )


@workspace_router.post(
    "/workspaces/{workspace_id}/diagnostic",
    response_model=WorkspaceDiagnosticResult,
    summary="Run a short read-only diagnostic command on a workspace",
    responses=common_responses(400, 404, 422, 500),
)
async def diagnostic_workspace(
    body: DiagnosticExecBody,
    workspace_id: str = Path(..., description="Workspace id"),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> WorkspaceDiagnosticResult:
    """Run a whitelisted shell command against the workspace and return
    stdout/stderr/exit_code. Used by the UI for a hello-world reachability
    smoke. Rejects any command whose head token is not on the whitelist.
    """
    # Whitelist check lives in the route (not in diagnostic_exec) so the
    # backend method stays a thin shell-pass-through; the SAFETY layer
    # is owned by the public surface.
    head = body.command.strip().split(None, 1)[0] if body.command.strip() else ""
    if head not in _DIAGNOSTIC_COMMAND_WHITELIST:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "command_not_whitelisted",
                "head": head,
                "allowed": sorted(_DIAGNOSTIC_COMMAND_WHITELIST),
                "message": (
                    f"diagnostic command head {head!r} is not on the "
                    "whitelist; allowed commands are: "
                    f"{sorted(_DIAGNOSTIC_COMMAND_WHITELIST)}"
                ),
            },
        )
    ws = await registry.get_workspace(workspace_id)
    timeout = body.timeout_seconds if body.timeout_seconds is not None else 5.0
    try:
        return await ws.diagnostic_exec(body.command, timeout_seconds=timeout)
    except NotImplementedError as exc:
        # Sandbox/K8s backends that don't yet wire diagnostic_exec
        # through their runtime surface this as 501 so the UI can show
        # a clear "not supported" message instead of a 500.
        raise HTTPException(
            status_code=501,
            detail={
                "error": "not_implemented",
                "message": str(exc) or (
                    "diagnostic_exec is not implemented for this workspace "
                    "backend"
                ),
            },
        ) from exc


# ===========================================================================
# Sessions sub-resource
# ===========================================================================

sessions_router = APIRouter(tags=["workspace-sessions"])


@sessions_router.get(
    "/workspaces/{workspace_id}/sessions",
    summary="List sessions on a workspace",
    responses=common_responses(404, 500),
)
async def list_sessions(
    workspace_id: str = Path(..., description="Workspace id"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> dict:
    ws = await registry.get_workspace(workspace_id)
    sessions = await ws.list_sessions()
    sliced = sessions[offset : offset + limit]
    return {
        "items": [s.model_dump(mode="json") for s in sliced],
        "offset": offset,
        "length": len(sliced),
        "total": len(sessions),
    }


@sessions_router.get(
    "/workspaces/{workspace_id}/sessions/{session_id}",
    summary="Get session state",
    responses=common_responses(404, 500),
)
async def get_session(
    workspace_id: str = Path(..., description="Workspace id"),
    session_id: str = Path(..., description="Session id"),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> dict:
    ws = await registry.get_workspace(workspace_id)
    session = await ws.get_session(session_id)
    if session is None:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    info = await session.info()
    status = await session.status()
    return {
        "info": info.model_dump(mode="json"),
        "status": status.value if hasattr(status, "value") else str(status),
    }


@sessions_router.patch(
    "/workspaces/{workspace_id}/sessions/{session_id}",
    summary="Rename a session (set its friendly display name)",
    responses=common_responses(404, 422, 500),
)
async def rename_session(
    body: SessionRenameBody = Body(...),
    workspace_id: str = Path(..., description="Workspace id"),
    session_id: str = Path(..., description="Session id"),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
    session_storage=Depends(get_session_storage),
) -> dict:
    """Set (or clear) a session's friendly name.

    Rewrites the ``name`` on the on-disk :class:`SessionInfo`
    (``session.json``) via :meth:`AgentSession.set_name` — the authoritative
    display source for the workspace sessions list — and best-effort mirrors
    it onto the scheduler-visible :class:`WorkspaceSession` row so the
    top-level ``GET /sessions/{id}`` read agrees. An empty / null name clears
    the label (the console falls back to the id). Returns the updated
    :class:`SessionInfo`.
    """
    ws = await registry.get_workspace(workspace_id)
    session = await ws.get_session(session_id)
    if session is None:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    info = await session.set_name(body.name)

    # Best-effort: mirror the new name onto the scheduler row so reads that
    # go through WorkspaceSession storage (the top-level GET /sessions/{id}
    # and the Studio center panel) reflect the rename too. A missing row
    # (e.g. an on-disk-only session) must not fail the rename. session_storage
    # is the Storage[WorkspaceSession] (see get_session_storage).
    try:
        row = await session_storage.get(session_id)
        if row is not None and row.workspace_id == workspace_id:
            row.name = info.name
            await session_storage.update(row)
    except Exception as exc:  # noqa: BLE001 — advisory mirror, never fatal
        logger.warning(
            "rename_session: failed to mirror name onto scheduler row",
            extra={
                "session_id": session_id,
                "workspace_id": workspace_id,
                "exception": type(exc).__name__,
                "error": str(exc),
            },
        )

    return info.model_dump(mode="json")


@sessions_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/steer",
    response_model=WorkspaceSession,
    summary="Send a message — invoke / steer / resume (auto-wake)",
    responses=common_responses(404, 409, 422, 500),
)
async def steer_session(
    body: SteerBody,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
    scheduler=Depends(get_scheduler),
    engine=Depends(get_claim_engine),
    storage_provider=Depends(get_storage_provider),
    event_bus=Depends(get_event_bus),
) -> WorkspaceSession:
    """Send a user message to a session and auto-wake it.

    One input, four behaviours (studio-agents-interact §5.1): a message to
    a CREATED session invokes it; to a RUNNING/WAITING session it queues as
    the next turn (steer); to a PAUSED session it resumes; to an ENDED
    session it reopens it as a fresh invocation (divider + run). 409 only
    when an ENDED session is non-restartable (workspace_lost/force_deleted).
    """
    from primer.session.enqueue import SessionWakeDeps, wake_session

    deps = SessionWakeDeps(
        storage_provider=storage_provider,
        scheduler=scheduler,
        claim_engine=engine,
        workspace_registry=registry,
        event_bus=event_bus,
    )
    return await wake_session(
        workspace_id=workspace_id,
        session_id=session_id,
        instruction=body.instruction,
        deps=deps,
    )


class RestartBody(BaseModel):
    """Body of ``POST /v1/workspaces/{id}/sessions/{sid}/restart``."""

    input: str | None = Field(
        default=None,
        description=(
            "Optional new initial input to invoke the re-opened session "
            "with. Omit to re-open and invoke with the existing queued "
            "state only."
        ),
    )


@sessions_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/restart",
    response_model=WorkspaceSession,
    summary="Reset an ended session and re-invoke (reset-same-session + wake)",
    responses=common_responses(404, 409, 422, 500),
)
async def restart_session_route(
    body: RestartBody,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
    scheduler=Depends(get_scheduler),
    engine=Depends(get_claim_engine),
    storage_provider=Depends(get_storage_provider),
    event_bus=Depends(get_event_bus),
) -> WorkspaceSession:
    """Re-open an ENDED session and invoke it (studio-agents-interact §5.3)."""
    from primer.session.enqueue import SessionWakeDeps
    from primer.session.reset import SessionResetDeps, restart_session

    return await restart_session(
        workspace_id=workspace_id,
        session_id=session_id,
        instruction=body.input,
        reset_deps=SessionResetDeps(
            storage_provider=storage_provider,
            workspace_registry=registry,
            event_bus=event_bus,
        ),
        wake_deps=SessionWakeDeps(
            storage_provider=storage_provider,
            scheduler=scheduler,
            claim_engine=engine,
            workspace_registry=registry,
            event_bus=event_bus,
        ),
    )


@sessions_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/interrupt",
    response_model=WorkspaceSession,
    summary="Stop the in-flight turn but keep the session alive",
    responses=common_responses(404, 409, 500),
)
async def interrupt_session(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    sessions=Depends(get_session_storage),
    event_bus=Depends(get_event_bus),
) -> WorkspaceSession:
    """Stop (interrupt) the running turn without ending the session.

    RUNNING: flag ``interrupt_requested`` + publish ``session:{sid}:cancel``
    so the worker preempts the turn and lands the session in WAITING (alive).
    Non-running: 200 no-op. ENDED: 409 (studio-agents-interact §4.4).
    """
    async with session_lifecycle_lock().acquire(session_id):
        s = await sessions.get(session_id)
        if s is None or s.workspace_id != workspace_id:
            raise NotFoundError(
                f"Session {session_id!r} does not exist on workspace "
                f"{workspace_id!r}"
            )
        if s.status == SessionStatus.ENDED:
            raise ConflictError(f"Session {session_id!r} has ended")
        if s.status == SessionStatus.RUNNING:
            s.interrupt_requested = True
            s.cancel_requested_at = datetime.now(timezone.utc)
            await sessions.update(s)
            if event_bus is not None:
                try:
                    await event_bus.publish(
                        f"session:{session_id}:cancel", {}
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "interrupt_session: bus publish failed for %s",
                        session_id,
                    )
        return s


# ===========================================================================
# Files sub-resource
# ===========================================================================

files_router = APIRouter(tags=["workspace-files"])


@files_router.get(
    "/workspaces/{workspace_id}/files/tree",
    summary="Return a one-level directory tree",
    responses=common_responses(400, 404, 500),
)
async def file_tree(
    workspace_id: str = Path(...),
    path: str = Query(default=".", description="Workspace-relative path"),
    depth: int = Query(default=1, ge=1, description="Tree depth (only depth=1 is supported; deeper values are accepted but treated as 1)"),
    hidden: bool = Query(default=False, description="Include hidden entries (e.g. .state)"),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> dict:
    ws = await registry.get_workspace(workspace_id)
    entries = await ws.list_files(path, recursive=False)
    items = []
    for entry in entries:
        name = entry.path.rsplit("/", 1)[-1] if "/" in entry.path else entry.path
        if not hidden and (entry.path == ".state" or entry.path.endswith("/.state")):
            continue
        items.append(
            {
                "name": name,
                "path": entry.path,
                "is_dir": entry.kind == "dir",
                "size_bytes": entry.size_bytes,
                "mtime": entry.modified_at.timestamp(),
                "mtime_iso": entry.modified_at.isoformat(),
            }
        )
    items.sort(key=lambda x: (0 if x["is_dir"] else 1, x["name"]))
    return {"path": path, "items": items}


@files_router.get(
    "/workspaces/{workspace_id}/files",
    summary="List files at a workspace path",
    responses=common_responses(400, 404, 500),
)
async def list_files(
    workspace_id: str = Path(...),
    path: str = Query(default=".", description="Workspace-relative path"),
    recursive: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> dict:
    ws = await registry.get_workspace(workspace_id)
    entries = await ws.list_files(path, recursive=recursive)
    sliced = entries[offset : offset + limit]
    return {
        "items": [e.model_dump(mode="json") for e in sliced],
        "offset": offset,
        "length": len(sliced),
        "total": len(entries),
        "path": path,
    }


@files_router.get(
    "/workspaces/{workspace_id}/files/info",
    response_model=FileEntry,
    summary="Get info for a single file or directory",
    responses=common_responses(400, 404, 500),
)
async def file_info(
    workspace_id: str = Path(...),
    path: str = Query(..., description="Workspace-relative path"),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> FileEntry:
    ws = await registry.get_workspace(workspace_id)
    return await ws.file_info(path)


@files_router.get(
    "/workspaces/{workspace_id}/files/read",
    response_model=FileReadResponse,
    summary="Read a file's content",
    responses=common_responses(400, 404, 500),
)
async def read_file(
    workspace_id: str = Path(...),
    path: str = Query(..., description="Workspace-relative path"),
    encoding: Literal["text", "base64"] = Query(
        default="text",
        description=(
            "How to encode the response payload. ``text`` UTF-8 decodes "
            "the bytes; ``base64`` returns the raw bytes as base64."
        ),
    ),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> FileReadResponse:
    ws = await registry.get_workspace(workspace_id)
    raw = await ws.read_file(path)
    if encoding == "text":
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BadRequestError(
                f"file at {path!r} is not valid UTF-8; request "
                "encoding=base64 instead"
            ) from exc
    else:
        content = base64.b64encode(raw).decode("ascii")
    entry = await ws.file_info(path)
    mtime_iso = entry.modified_at.isoformat()
    mtime = entry.modified_at.timestamp()
    etag = hashlib.md5(f"{mtime_iso}:{len(raw)}".encode()).hexdigest()
    return FileReadResponse(
        path=path,
        encoding=encoding,
        content=content,
        size_bytes=len(raw),
        mtime=mtime,
        mtime_iso=mtime_iso,
        etag=etag,
    )


@files_router.get(
    "/workspaces/{workspace_id}/files/download",
    summary="Download a file's raw bytes",
    responses=common_responses(400, 404, 500),
)
async def download_file(
    workspace_id: str = Path(...),
    path: str = Query(..., description="Workspace-relative path"),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> StreamingResponse:
    ws = await registry.get_workspace(workspace_id)
    raw = await ws.read_file(path)

    async def _gen():
        yield raw

    filename = _safe_attachment_filename(path)
    return StreamingResponse(
        _gen(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(filename),
            "Content-Length": str(len(raw)),
        },
    )


@files_router.post(
    "/workspaces/{workspace_id}/files/dir",
    status_code=204,
    summary="Create a directory (and any missing parents)",
    responses=common_responses(400, 404, 500),
)
async def make_dir(
    workspace_id: str = Path(...),
    path: str = Query(..., description="Workspace-relative path"),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> None:
    ws = await registry.get_workspace(workspace_id)
    await ws.make_dir(path)


@files_router.delete(
    "/workspaces/{workspace_id}/files",
    status_code=204,
    summary="Delete a file or directory",
    responses=common_responses(400, 404, 500),
)
async def delete_file(
    workspace_id: str = Path(...),
    path: str = Query(..., description="Workspace-relative path"),
    recursive: bool = Query(
        default=False,
        description="Delete a non-empty directory and all its contents",
    ),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> None:
    ws = await registry.get_workspace(workspace_id)
    await ws.delete_file(path, recursive=recursive)


@files_router.post(
    "/workspaces/{workspace_id}/files/move",
    status_code=204,
    summary="Move or rename a file or directory within the workspace",
    responses=common_responses(400, 404, 409, 500),
)
async def move_file(
    workspace_id: str = Path(...),
    src: str = Query(..., description="Source workspace-relative path"),
    dst: str = Query(..., description="Destination workspace-relative path"),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> None:
    """Move / rename ``src`` to ``dst`` within one workspace.

    Query params ``src`` + ``dst`` mirror the other file endpoints' use of
    ``path``. The backend enforces the safety envelope (root-relative, no
    reserved-tree escape, no clobber of an existing ``dst``, no dir-into-its-
    own-descendant); violations surface as 400 / 404 / 409. Backends that do
    not implement move return 501.
    """
    ws = await registry.get_workspace(workspace_id)
    try:
        await ws.move_file(src, dst)
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=501,
            detail={
                "error": "not_implemented",
                "message": str(exc) or (
                    "move_file is not implemented for this workspace backend"
                ),
            },
        ) from exc


@files_router.put(
    "/workspaces/{workspace_id}/files",
    status_code=204,
    summary="Replace (or create) a file's contents",
    responses={
        **common_responses(400, 422, 500),
        412: {
            "model": ProblemDetails,
            "description": "Precondition Failed",
            "content": {PROBLEM_JSON_MEDIA_TYPE: {}},
        },
    },
)
async def write_file(
    request: Request,
    workspace_id: str = Path(...),
    path: str = Query(..., description="Workspace-relative path"),
    body: FileWriteBody = Body(...),
    etag: str | None = Query(
        default=None,
        description="Optimistic-concurrency etag from a prior read response",
    ),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
    scheduler=Depends(get_scheduler),
    event_bus=Depends(get_event_bus),
) -> Response:
    ws = await registry.get_workspace(workspace_id)
    if body.encoding == "text":
        try:
            raw = body.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            # Lone surrogates and other unencodable characters arrive
            # via JSON `\uXXXX` escapes; reject as invalid input rather
            # than crashing the request.
            raise BadRequestError(
                f"text content is not valid UTF-8: {exc}"
            ) from exc
    else:
        try:
            raw = base64.b64decode(body.content, validate=True)
        except Exception as exc:  # noqa: BLE001 — base64.binascii.Error
            raise BadRequestError(f"invalid base64 content: {exc}") from exc
    if_unmodified_since_hdr = request.headers.get("if-unmodified-since")
    if etag is not None or if_unmodified_since_hdr is not None:
        try:
            entry = await ws.file_info(path)
        except NotFoundError:
            entry = None
        if entry is not None:
            conflict = False
            if etag is not None:
                current_etag = hashlib.md5(
                    f"{entry.modified_at.isoformat()}:{entry.size_bytes}".encode()
                ).hexdigest()
                if etag != current_etag:
                    conflict = True
            elif if_unmodified_since_hdr is not None:
                try:
                    parsed_date = email.utils.parsedate_to_datetime(
                        if_unmodified_since_hdr
                    )
                    if entry.modified_at > parsed_date:
                        conflict = True
                except Exception:  # noqa: BLE001 — ignore malformed header
                    pass
            if conflict:
                problem = ProblemDetails(
                    type="/errors/precondition-failed",
                    title="Precondition Failed",
                    status=412,
                    detail="The file has been modified since the precondition was recorded.",
                    instance=request.url.path,
                )
                return JSONResponse(
                    status_code=412,
                    content=problem.model_dump(exclude_none=True),
                    media_type=PROBLEM_JSON_MEDIA_TYPE,
                )
    await ws.write_file(path, raw)
    # Deterministically wake any watch_files-parked session in this
    # workspace whose watched paths match the just-written file. This
    # reuses the event-bus -> YieldEventListener resume path; inotify
    # stays as the backstop. Best-effort: a wake failure must never fail
    # the write itself.
    try:
        from primer.bus.watch_notify import wake_watch_files_on_write

        await wake_watch_files_on_write(
            workspace_id=workspace_id,
            path=path,
            scheduler=scheduler,
            event_bus=event_bus,
        )
    except Exception:  # noqa: BLE001 — wake is best-effort
        logger.exception(
            "wake_watch_files_on_write failed for workspace=%r path=%r",
            workspace_id,
            path,
        )
    return Response(status_code=204)


# ===========================================================================
# Log sub-resource
# ===========================================================================

log_router = APIRouter(tags=["workspace-log"])


@log_router.get(
    "/workspaces/{workspace_id}/log",
    summary="Workspace state-repo git log",
    responses=common_responses(404, 500),
)
async def workspace_log(
    workspace_id: str = Path(...),
    limit: int = Query(default=50, ge=1, le=500),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> dict:
    ws = await registry.get_workspace(workspace_id)
    commits = await ws.log(limit=limit)
    return {"commits": [c.model_dump(mode="json") for c in commits]}


@log_router.get(
    "/workspaces/{workspace_id}/commit/{sha}",
    summary="Show one commit: header + per-file unified diff",
    responses=common_responses(404, 500),
)
async def workspace_show_commit(
    workspace_id: str = Path(...),
    sha: str = Path(..., min_length=7, max_length=64),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> dict:
    """Return the diff payload for a single commit in the workspace
    state repo. The returned shape is
    ``{sha, subject, body, parent, files: [{path, status, patch}]}``.
    """
    ws = await registry.get_workspace(workspace_id)
    state_repo = getattr(ws, "_state", None)
    show = getattr(state_repo, "show_commit", None) if state_repo else None
    if show is None:
        raise HTTPException(
            status_code=501,
            detail={
                "error": "not_implemented",
                "message": (
                    "Backend does not expose a state-repo show_commit "
                    "hook. Only local + container-state backends support "
                    "diff inspection today."
                ),
            },
        )
    try:
        return await show(sha)
    except FileNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


# ===========================================================================
# Yields pending sub-resource (Studio A3)
# ===========================================================================

yields_pending_router = APIRouter(tags=["workspace-yields"])


def _extract_yield_kind(tool_name: str) -> str:
    """Map the internal ``tool_name`` stored in the parked_state blob to the
    human-facing ``kind`` field exposed in the API response.

    * ``_approval`` → ``"approval"`` (tool-approval gate)
    * everything else is returned verbatim (``ask_user``, ``watch_files``,
      ``sleep``, ``invoke_graph``, …).
    """
    if tool_name == "_approval":
        return "approval"
    return tool_name


def _extract_yield_prompt(tool_name: str, metadata: dict) -> str:
    """Return the best human-facing description for a parked yield.

    ``metadata`` is ``blob["yielded"]["resume_metadata"]`` (or ``{}``).

    Per-kind extraction:
    * ``ask_user``   → the ``prompt`` string the agent emitted.
    * ``_approval``  → ``original_call.name`` (the tool awaiting approval).
    * ``watch_files``→ the watched paths joined as a comma-separated string.
    * ``sleep``      → ``"<N>s"`` from ``requested_seconds``.
    * others         → empty string (callers may flag as unknown).
    """
    if tool_name == "ask_user":
        return str(metadata.get("prompt") or "")
    if tool_name == "_approval":
        original = metadata.get("original_call") or {}
        return str(original.get("name") or "")
    if tool_name == "watch_files":
        paths = metadata.get("paths") or []
        return ", ".join(str(p) for p in paths)
    if tool_name == "sleep":
        secs = metadata.get("requested_seconds")
        if secs is not None:
            return f"{secs}s"
        return ""
    return ""


def _tool_call_id_from_blob(blob: dict) -> str | None:
    """Resolve the tool_call_id from a raw parked_state blob.

    Mirrors the logic in ``yields.py::_tool_call_id_for``: top-level key
    first, then fallback into ``yielded.resume_metadata``.
    """
    tcid = blob.get("tool_call_id")
    if tcid:
        return str(tcid)
    yielded = blob.get("yielded") or {}
    meta = yielded.get("resume_metadata") or {}
    tcid = meta.get("tool_call_id")
    return str(tcid) if tcid else None


@yields_pending_router.get(
    "/workspaces/{workspace_id}/yields/pending",
    summary="Aggregated pending yields across all sessions (Studio Action Required)",
    responses=common_responses(404, 500),
)
async def list_pending_yields(
    workspace_id: str = Path(..., description="Workspace id"),
    session_storage=Depends(get_session_storage),
) -> dict:
    """Return every pending yield across **all** parked sessions in the
    workspace.

    Drives the Studio right-sidebar "Action Required" panel on load;
    live deltas arrive via ``yielded``/``done`` tap events.

    Response shape::

        {
            "items": [
                {
                    "session_id": str,
                    "kind": "ask_user" | "approval" | "watch_files" | "sleep" | …,
                    "prompt": str,           # human-facing description
                    "tool_call_id": str | null,
                    "parked_at": str | null  # ISO-8601
                },
                …
            ]
        }

    Only sessions with ``parked_status == "parked"`` are included; running
    and ended sessions are excluded. Sessions from other workspaces are never
    returned.
    """
    from primer.model.storage import OffsetPage
    from primer.storage.q import Q
    from primer.model.workspace_session import WorkspaceSession

    predicate = (
        Q(WorkspaceSession)
        .where("workspace_id", workspace_id)
        .where("parked_status", "parked")
        .build()
    )

    items = []
    offset = 0
    page_size = 200
    while True:
        resp = await session_storage.find(
            predicate,
            OffsetPage(offset=offset, length=page_size),
        )
        for sess in resp.items:
            blob: dict = sess.parked_state or {}
            yielded_blob: dict = blob.get("yielded") or {}
            tool_name: str = yielded_blob.get("tool_name") or ""
            metadata: dict = yielded_blob.get("resume_metadata") or {}

            kind = _extract_yield_kind(tool_name)
            prompt = _extract_yield_prompt(tool_name, metadata)
            tcid = _tool_call_id_from_blob(blob)
            parked_at = (
                sess.parked_at.isoformat() if sess.parked_at is not None else None
            )

            items.append(
                {
                    "session_id": sess.id,
                    "kind": kind,
                    "prompt": prompt,
                    "tool_call_id": tcid,
                    "parked_at": parked_at,
                }
            )
        if len(resp.items) < page_size:
            break
        offset += page_size

    return {"items": items}


@yields_pending_router.get(
    "/workspaces/{workspace_id}/sessions/{session_id}/yields/pending",
    summary="Pending yields for one session (inline session-stream affordances)",
    responses=common_responses(404, 500),
)
async def list_session_pending_yields(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    session_storage=Depends(get_session_storage),
) -> dict:
    """Return the pending yield(s) for a single session.

    Same item shape as the aggregated ``/workspaces/{wid}/yields/pending``
    but scoped to one session, so the run-view can render Approve/Deny /
    respond affordances inline in the stream while the right sidebar keeps
    the global Action-Required list (studio-agents-interact §5.4 / §4.5).
    """
    sess = await session_storage.get(session_id)
    if sess is None or sess.workspace_id != workspace_id:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    items: list[dict] = []
    if sess.parked_status == "parked":
        blob: dict = sess.parked_state or {}
        yielded_blob: dict = blob.get("yielded") or {}
        tool_name: str = yielded_blob.get("tool_name") or ""
        metadata: dict = yielded_blob.get("resume_metadata") or {}
        items.append({
            "session_id": sess.id,
            "kind": _extract_yield_kind(tool_name),
            "prompt": _extract_yield_prompt(tool_name, metadata),
            "tool_call_id": _tool_call_id_from_blob(blob),
            "parked_at": (
                sess.parked_at.isoformat()
                if sess.parked_at is not None else None
            ),
        })
    return {"items": items}


# ===========================================================================
# Workspace events history sub-resource (Studio activity backfill)
# ===========================================================================

events_router = APIRouter(tags=["workspace-events"])


@events_router.get(
    "/workspaces/{workspace_id}/events",
    summary="Recent workspace-scoped tap events across all sessions (Studio activity backfill)",
    responses=common_responses(404, 500),
)
async def list_workspace_events(
    workspace_id: str = Path(..., description="Workspace id"),
    limit: int = Query(
        default=200,
        ge=1,
        le=500,
        description=(
            "Maximum number of most-recent events to return, aggregated across "
            "all of the workspace's sessions."
        ),
    ),
    session_storage=Depends(get_session_storage),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> dict:
    """Return the most-recent ``limit`` tap events across ALL sessions in the
    workspace, oldest-first.

    The workspace tap SSE stream connects **live-from-now**, so a panel that
    opens after events already happened (e.g. a completed session) sees nothing.
    This bounded backfill seeds the Studio activity stream on open; the live tap
    then tails from now and the client dedupes the seam by ``(session_id, seq)``.

    Each item is a wire-shape :class:`~primer.tap.event.TapEvent`
    (``class`` / ``ts`` / ``seq`` / ``session_id`` / ``payload`` …) so it merges
    1:1 with live tap frames — the same reader (:func:`read_session_since`) that
    backs the SSE tick loop produces these, just drained from byte 0.

    Response shape::

        {"items": [ {"class": str, "seq": int, "session_id": str,
                     "ts": str, "payload": {...}, ...}, … ]}

    Events are ordered ascending by ``(ts, session_id, seq)`` and the newest
    ``limit`` are returned. A missing ``messages.jsonl`` (a session that has not
    flushed yet) contributes nothing rather than erroring.
    """
    from primer.model.storage import OffsetPage
    from primer.storage.q import Q
    from primer.model.workspace_session import WorkspaceSession
    from primer.tap.reader import read_session_since
    from primer.tap.selector import TapSelector

    # Resolve the live workspace IO handle (read_file + state_path). A missing
    # workspace raises NotFoundError → 404, mirroring the tap SSE surface.
    workspace_io = await registry.get_workspace(workspace_id)

    predicate = Q(WorkspaceSession).where("workspace_id", workspace_id).build()
    selector = TapSelector()  # empty = pass-through (every session + event)

    collected = []
    offset = 0
    page_size = 200
    while True:
        resp = await session_storage.find(
            predicate, OffsetPage(offset=offset, length=page_size)
        )
        for sess in resp.items:
            events, _ = await read_session_since(
                workspace_io,
                workspace_id=workspace_id,
                session=sess,
                after_seq=0,
                selector=selector,
                from_offset=0,
            )
            # Keep only each session's most-recent `limit` events: the global
            # recent-N is a subset of the union of per-session tails, so this
            # bounds memory without dropping any event that could land in the
            # final window.
            if len(events) > limit:
                events = events[-limit:]
            collected.extend(events)
        if len(resp.items) < page_size:
            break
        offset += page_size

    # Global order by (ts, session_id, seq); return the most-recent `limit`
    # oldest-first so the client appends them like the live tail.
    collected.sort(key=lambda e: (e.ts, e.session_id, e.seq))
    recent = collected[-limit:]
    return {"items": [ev.model_dump(mode="json", by_alias=True) for ev in recent]}


# ===========================================================================
# Helpers
# ===========================================================================


import re as _re
from urllib.parse import quote as _urlquote

# RFC 6266: filenames in `Content-Disposition: attachment; filename=...`
# must be quoted; characters outside this safe set get either stripped
# (in the legacy ``filename=`` parameter) or percent-encoded (via
# RFC 5987 ``filename*``). The strict ``filename=`` value uses only
# this set so a malicious basename cannot inject a CR/LF (header
# injection) or break out of the quoted string.
_SAFE_FILENAME_CHARS = _re.compile(r"[^A-Za-z0-9._\- ]")


def _safe_attachment_filename(path: str) -> str:
    """Strip the basename of a workspace-relative path down to a
    header-injection-proof ASCII slug. Empty results fall back to
    ``"download"``."""
    base = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = _SAFE_FILENAME_CHARS.sub("_", base).strip(". ")
    return base or "download"


def _content_disposition(filename: str) -> str:
    """Build a Content-Disposition header that carries both the
    ASCII-only ``filename=`` (for legacy clients) and an RFC 5987
    ``filename*`` parameter (UTF-8) so non-ASCII filenames survive."""
    encoded = _urlquote(filename, safe="")
    return (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{encoded}"
    )


__all__ = [
    "events_router",
    "files_router",
    "log_router",
    "provider_router",
    "sessions_router",
    "template_router",
    "workspace_router",
    "yields_pending_router",
]
