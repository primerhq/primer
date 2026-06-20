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
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_storage_provider,
    get_workspace_provider_storage,
    get_workspace_registry,
    get_workspace_storage,
    get_workspace_template_storage,
)
from primer.api.errors import common_responses
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


class _ChannelAssociationBody(BaseModel):
    """Body of ``PUT /v1/workspaces/{id}/channel_association``."""

    channel_id: str = Field(..., min_length=1, description="Channel id to associate.")


@workspace_router.put(
    "/workspaces/{workspace_id}/channel_association",
    response_model=WorkspaceRow,
    summary="Set the channel association for a Workspace",
    responses=common_responses(404, 409, 422, 500),
)
async def set_workspace_channel_association(
    workspace_id: str = Path(..., description="Workspace id"),
    body: _ChannelAssociationBody = Body(...),
    workspace_storage=Depends(get_workspace_storage),
    sp=Depends(get_storage_provider),
) -> WorkspaceRow:
    """Attach a Channel to this workspace.

    After this call, session gates (ask_user / tool_approval) on this
    workspace forward to the designated channel. Validates that the
    channel exists and that the workspace is not in a terminating phase.
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
                    "cannot have its channel association changed."
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
    "/workspaces/{workspace_id}/channel_association",
    status_code=204,
    summary="Clear the channel association for a Workspace",
    responses=common_responses(404, 500),
)
async def clear_workspace_channel_association(
    workspace_id: str = Path(..., description="Workspace id"),
    workspace_storage=Depends(get_workspace_storage),
) -> None:
    """Detach the channel association from this workspace.

    After this call, session gates on this workspace are no longer
    forwarded to any channel. No-ops silently if the association was
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


@sessions_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/steer",
    summary="Append a steering user instruction",
    responses=common_responses(404, 409, 422, 500),
)
async def steer_session(
    body: SteerBody,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> dict:
    ws = await registry.get_workspace(workspace_id)
    session = await ws.get_session(session_id)
    if session is None:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    instruction = await session.append_instruction(body.instruction)
    return instruction.model_dump(mode="json")


# ===========================================================================
# Files sub-resource
# ===========================================================================

files_router = APIRouter(tags=["workspace-files"])


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
    return FileReadResponse(
        path=path, encoding=encoding, content=content, size_bytes=len(raw)
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


@files_router.put(
    "/workspaces/{workspace_id}/files",
    status_code=204,
    summary="Replace (or create) a file's contents",
    responses=common_responses(400, 404, 422, 500),
)
async def write_file(
    workspace_id: str = Path(...),
    path: str = Query(..., description="Workspace-relative path"),
    body: FileWriteBody = Body(...),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> None:
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
    await ws.write_file(path, raw)


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
    "files_router",
    "log_router",
    "provider_router",
    "sessions_router",
    "template_router",
    "workspace_router",
]
