"""Workspace REST surface - providers, templates, workspaces + sub-resources.

Three entity routers and three sub-resources on Workspace:

* ``WorkspaceProvider`` - list / get / create / update / delete. Reserved
  bootstrap-managed providers (see
  :data:`~primer.api.registries.provider_registry.RESERVED_WORKSPACE_PROVIDER_IDS`)
  are read-only: PUT and DELETE against a reserved id return 403.
* ``WorkspaceTemplate`` - full CRUD (list / get / create / update /
  delete).
* ``Workspace`` - list / get / create / delete (no update). Body of
  ``POST`` is :class:`WorkspaceCreateBody` (template id + optional
  overrides).

Sub-resources on ``/v1/workspaces/{id}``:

* Sessions - list, get, pause, resume, steer.
* Files - list (paginated ls), info, read, download, delete, write.
* Log - git log over the ``.state`` repo.
* Yields - aggregated pending yields across all sessions (Studio A3).
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import logging
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from primer.api.deps import (
    get_claim_engine,
    get_collection_storage,
    get_event_bus,
    get_optional_artifact_storage_registry,
    get_provider_registry,
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
from primer.model.common import preserve_masked_secrets
from primer.api.routers._references import ReferenceCheck
from primer.bootstrap.defaults import RESERVED_WORKSPACE_TEMPLATES
from primer.model.except_ import (
    ConfigError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationError as SemanticValidationError,
)
from primer.model.external_tool import (
    ExternalToolDef,
    ExternalToolResultIn,
    validate_external_tool_defs,
)
from primer.model.storage import (
    CursorPageResponse,
    OffsetPage,
    OffsetPageResponse,
    OrderBy,
    PageRequest,
)
from primer.model.workspace import (
    WorkspaceEventsConfig,
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
from primer.session.pending_gates import enumerate_pending_gates


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
            "string is permitted - it produces an empty file."
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
            "``uname``, ``ls``) - anything else is rejected with 400. "
            "This is a read-only diagnostic surface, not arbitrary RCE."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0.0,
        le=30.0,
        description=(
            "Per-call timeout ceiling. Defaults to 5.0 if omitted. "
            "Hard-capped at 30s - the route is for liveness smokes, "
            "not long-running jobs."
        ),
    )


_DIAGNOSTIC_COMMAND_WHITELIST: frozenset[str] = frozenset(
    {"echo", "pwd", "whoami", "uname", "ls"}
)


def _reject_path_escape(value: str) -> str:
    """Reject an absolute path or any ``..`` segment.

    Same check as :meth:`WorkspaceTemplate._validate_workspace_relative_path`
    (``primer/model/workspace.py``) -- kept as a standalone function here
    rather than importing that bound classmethod, since it validates an
    unrelated model. This is the WIRE-LAYER half of the traversal defence:
    it turns an escape attempt into a clean 422 before the request reaches
    ``media_from_workspace_files``, which independently re-derives the same
    guarantee at the filesystem layer via ``Workspace._resolve_path``
    (``candidate.relative_to(root)``) -- belt and suspenders, not a
    substitute for each other.
    """
    from pathlib import PurePosixPath, PureWindowsPath

    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValueError(
            f"attachment path {value!r} must be relative to the workspace "
            f"root, not absolute"
        )
    parts = value.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        raise ValueError(
            f"attachment path {value!r} must not contain '..' segments "
            f"(would escape the workspace root)"
        )
    return value


class AttachmentIn(BaseModel):
    """One workspace file to fold into a steer as vision/document input.

    The file must already exist in the workspace -- upload it first via
    the existing ``PUT /v1/workspaces/{id}/files`` (unchanged). Resolved
    server-side through :func:`primer.channel.media.media_from_workspace_files`,
    the same artifact-backed-Part pipeline ``ask_user``/``inform_user``
    already use for outbound files.
    """

    path: str = Field(
        ...,
        min_length=1,
        description="Workspace-relative path to an already-uploaded file.",
    )

    @model_validator(mode="after")
    def _safe_path(self) -> "AttachmentIn":
        _reject_path_escape(self.path)
        return self


class SteerBody(BaseModel):
    """Body of ``POST /v1/workspaces/{id}/sessions/{sid}/steer``.

    One endpoint, all invocation behaviours: ``instruction`` invokes /
    steers / resumes; ``tool_results`` resolves pending external tool
    calls; both together mean results first, cancel any remaining
    pending calls, then the instruction steers the resumed turn.
    ``external_tools`` registers invoker-supplied tool defs for the
    turn this message triggers (gated by the agent's
    ``allow_external_tools``). ``attachments`` folds already-uploaded
    workspace files into the turn as true vision/document input,
    alongside the existing plain-text ``"Attached file: {path}"``
    convention the composer also supports.
    """

    response_format: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Structured-output JSON Schema for THIS turn only. Beats the "
            "session's persistent response_format, which beats the "
            "agent default. Consumed once: a retry of the same turn "
            "falls back rather than silently re-applying it."
        ),
    )
    instruction: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "User-role text appended as a fresh ``user_instruction`` "
            "message in the session's transcript."
        ),
    )
    external_tools: list[ExternalToolDef] | None = Field(
        default=None,
        description=(
            "Invoker-supplied tool defs for the turn this message "
            "triggers; replaces the session's active set. Omit to leave "
            "the set unchanged on a pure-results body; [] clears it."
        ),
    )
    tool_results: list[ExternalToolResultIn] | None = Field(
        default=None,
        description=(
            "Results for pending external tool calls. Unknown or "
            "already-resolved ids reject the whole request (409) before "
            "any state changes."
        ),
    )
    attachments: list[AttachmentIn] | None = Field(
        default=None,
        description=(
            "Workspace files to fold into this turn's user message as "
            "true vision/document input. Requires 'instruction' -- an "
            "attachment needs a user turn to ride in on. A missing/"
            "oversized/disallowed-type file is dropped with a log rather "
            "than failing the whole steer (matches the ask_user/"
            "inform_user files= convention)."
        ),
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> "SteerBody":
        if not self.instruction and not self.tool_results:
            raise ValueError(
                "steer body needs 'instruction' and/or 'tool_results'"
            )
        if self.tool_results is not None and len(self.tool_results) == 0:
            raise ValueError("'tool_results' must be non-empty when present")
        if self.attachments and not self.instruction:
            raise ValueError(
                "'attachments' requires 'instruction' -- an attachment "
                "needs a user turn to ride in on"
            )
        if self.external_tools:
            validate_external_tool_defs(self.external_tools)
        return self


async def _binding_allows_external(row, storage_provider) -> bool:
    """Does this session's agent allow invoker-supplied tools?

    Prefers the binding's frozen ``agent_snapshot`` when one was taken
    (immutability against later Agent edits, matching every other
    snapshot-covered field); otherwise reads the live Agent row, the
    same live-definition semantics all non-snapshot sessions get. Graph
    bindings answer False here; the graph surface gates per node at
    injection time.
    """
    binding = getattr(row, "binding", None)
    if getattr(binding, "kind", None) != "agent":
        return False
    snap = getattr(binding, "agent_snapshot", None)
    if snap is not None:
        return bool(getattr(snap, "allow_external_tools", False))
    from primer.model.agent import Agent

    agent = await storage_provider.get_storage(Agent).get(binding.agent_id)
    return bool(agent is not None and agent.allow_external_tools)


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
    """Reject PUT /v1/workspace_providers/<reserved-id> (403); otherwise
    preserve any secret field (e.g. a Postgres-backed config's password)
    the PUT never actually touched.

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
    # 01a05198: see primer.model.common.preserve_masked_secrets.
    preserve_masked_secrets(entity, existing)


# ReferenceCheck.child_storage takes the raw Request (the FastAPI deps
# above are Depends-shaped and receive a StorageProvider instead).
def _ref_template_storage(request: Request):
    return get_storage_provider(request).get_storage(WorkspaceTemplate)


def _ref_workspace_storage(request: Request):
    return get_storage_provider(request).get_storage(WorkspaceRow)


provider_router = make_crud_router(
    model_cls=WorkspaceProvider,
    storage_dep=get_workspace_provider_storage,
    plural="workspace_providers",
    tag="workspace-providers",
    on_delete=_invalidate_workspace_backend,
    on_pre_create=_reject_reserved_workspace_provider_create,
    on_pre_update=_reject_reserved_workspace_provider_update,
    on_pre_delete_id=_reject_reserved_workspace_provider_delete,
    # Found by the 2026-08-24 BDD pass: deleting a provider that a
    # template (or a materialized workspace) still referenced answered
    # 204 and left every dependent workspace unable to even create a
    # session (the resolve 404s). Referenced entities refuse deletion.
    references=[
        ReferenceCheck(
            child_kind="workspace_template",
            child_storage=_ref_template_storage,
            child_field="provider_id",
        ),
        ReferenceCheck(
            child_kind="workspace",
            child_storage=_ref_workspace_storage,
            child_field="provider_id",
        ),
    ],
)


# ===========================================================================
# Template router (full CRUD)
# ===========================================================================

# Reserved template ids - bootstrapped by BootstrapRunner on first boot
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
    # Deliberately NO reference guard here: a template is a snapshot
    # consumed at materialization (spec section 12, pinned by e2e
    # T0223) - deleting it must not strand live workspaces, and they
    # keep working without it. Only the provider router above guards,
    # because a provider IS resolved live at session provisioning.
)


# ===========================================================================
# Workspace router (CRUD minus update; create + delete are bespoke)
# ===========================================================================

workspace_router = APIRouter(tags=["workspaces"])

_PageResp = OffsetPageResponse[Any] | CursorPageResponse[Any]

_SESSION_COUNT_PAGE_SIZE = 200


class WorkspaceRowWithUsage(WorkspaceRow):
    """Response-only shape: a Workspace plus its live session count.

    Never persisted -- session_count is computed fresh on every
    GET /v1/workspaces (bounded scan over Session storage, tallied by
    workspace_id in memory), not a stored field, so it carries no
    MIGRATIONS implication. Mirrors ModelProfileWithUsage's agent_count/
    graph_node_count pattern (model_profiles.py) -- same "always bounded
    scans, never N+1" trade-off.
    """

    session_count: int = Field(
        default=0,
        description=(
            "Number of WorkspaceSession rows whose workspace_id names "
            "this workspace -- every status, including ended/tombstoned "
            "sessions from workspaces that were later destroyed do NOT "
            "count here since their workspace_id no longer matches any "
            "row in this listing."
        ),
    )


async def _enrich_with_session_counts(
    resp: OffsetPageResponse | CursorPageResponse, request: Request,
) -> OffsetPageResponse | CursorPageResponse:
    """Attach a live session count to one page of workspaces."""
    session_storage = get_storage_provider(request).get_storage(WorkspaceSession)

    counts: Counter[str] = Counter()
    offset = 0
    while True:
        page = await session_storage.list(
            OffsetPage(offset=offset, length=_SESSION_COUNT_PAGE_SIZE),
        )
        for session in page.items:
            counts[session.workspace_id] += 1
        if len(page.items) < _SESSION_COUNT_PAGE_SIZE:
            break
        offset += _SESSION_COUNT_PAGE_SIZE

    enriched = [
        WorkspaceRowWithUsage(
            **item.model_dump(), session_count=counts.get(item.id, 0),
        )
        for item in resp.items
    ]
    return resp.model_copy(update={"items": enriched})


@workspace_router.get(
    "/workspaces",
    summary="List Workspaces",
    responses=common_responses(400, 422, 500),
)
async def list_workspaces(
    request: Request,
    page: PageRequest = Depends(parse_page),
    order_by: list[OrderBy] | None = Depends(parse_order_by),
    storage=Depends(get_workspace_storage),
) -> _PageResp:
    resp = await storage.list(page, order_by=order_by)
    return await _enrich_with_session_counts(resp, request)


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


@workspace_router.put(
    "/workspaces/{workspace_id}/events",
    response_model=WorkspaceRow,
    summary="Set the workspace's lifecycle-event streaming config",
    responses=common_responses(404, 500),
)
async def set_workspace_events(
    body: "WorkspaceEventsBody",
    workspace_id: str = Path(..., description="Workspace id"),
    storage=Depends(get_workspace_storage),
) -> WorkspaceRow:
    """Opt a workspace in or out of platform event streaming.

    When enabled, the WorkspaceEventBridge holds one runtime stream
    for this workspace and emits workspace.file_changed /
    exec_started / exec_exited onto the event log. ``config: null``
    switches it off.
    """
    row = await storage.get(workspace_id)
    if row is None:
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
    return await storage.update(
        row.model_copy(update={"events": body.config})
    )


class WorkspaceEventsBody(BaseModel):
    """Wrapper so ``config: null`` is expressible (clears the opt-in)."""

    config: WorkspaceEventsConfig | None = None


@workspace_router.put(
    "/workspaces/{workspace_id}/terminal_access",
    response_model=WorkspaceRow,
    summary="Grant or revoke non-admin access to the workspace's integrated terminal",
    responses=common_responses(404, 500),
)
async def set_workspace_terminal_access(
    body: "WorkspaceTerminalAccessBody",
    workspace_id: str = Path(..., description="Workspace id"),
    storage=Depends(get_workspace_storage),
) -> WorkspaceRow:
    """Toggle the per-workspace ``terminal_user_access`` flag.

    The integrated terminal (``WS /workspaces/{id}/terminal``) is
    admin-only by default; setting ``enabled: true`` here admits callers
    holding at least the ``user`` role too (``restricted`` accounts never
    get a shell regardless of this toggle).
    """
    row = await storage.get(workspace_id)
    if row is None:
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
    return await storage.update(
        row.model_copy(update={"terminal_user_access": body.enabled})
    )


class WorkspaceTerminalAccessBody(BaseModel):
    """Body of ``PUT /workspaces/{id}/terminal_access``."""

    enabled: bool


@workspace_router.post(
    "/workspaces",
    response_model=WorkspaceRow,
    status_code=201,
    summary="Create Workspace from template",
    responses=common_responses(404, 409, 422, 500),
)
async def create_workspace(
    body: WorkspaceCreateBody,
    request: Request,
    storage_provider=Depends(get_storage_provider),
    scheduler=Depends(get_scheduler),
    engine=Depends(get_claim_engine),
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

    # Reserve agent_sandbox slot - k8s provider variant=agent_sandbox is
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

    overrides = body.overrides or WorkspaceTemplateOverrides()
    # Pin the live instance to the caller-supplied id (same id the row is
    # keyed by, below) so re-attach after cache eviction resolves the SAME
    # backend object instead of 404ing.
    live = await registry.materialise(
        template=template, overrides=overrides, workspace_id=body.id
    )

    # materialise() has now created a live container/volume, but the durable
    # WorkspaceRow is written LAST. Any failure in the mount work or the
    # row-write in between would orphan that live instance: the probe loop is
    # row-driven and there is no orphan sweep, so it would leak silently
    # (arch review D-I1). Wrap everything after materialise so a failure best-
    # effort tears the live workspace back down before re-raising the original
    # error unchanged.
    try:
        row_id = body.id if body.id is not None else live.id
        # Mark the row "running" immediately - materialise() returned a live
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
    except BaseException:
        # BaseException, not Exception: asyncio.CancelledError has been a
        # BaseException since 3.8, so `except Exception` would skip the
        # teardown on the single most likely way this block is entered -- a
        # client disconnect during the slow materialise()+mount work cancels
        # the request task, which would orphan the very live instance this
        # rollback exists to reclaim (arch review D-I1). The bare `raise`
        # re-raises the original exception with its traceback intact and
        # preserves CancelledError's cancellation semantics.
        #
        # registry.destroy() is row-driven and the row does not exist yet, so
        # tear down the backend instance directly. Best-effort: never let a
        # cleanup failure mask the original error.
        try:
            backend = await registry.get_backend(template.provider_id)
            await backend.destroy(live.id)
        except Exception:
            logger.exception(
                "create_workspace: failed to roll back orphaned live "
                "workspace %s after a post-materialise error", live.id,
            )
        raise

    from primer.session.default_binding import resolve_initial_binding
    from primer.workspace.session_factory import (
        SessionFactoryDeps,
        create_session,
    )

    # A new workspace arrives with somewhere to talk. "main" is an
    # ordinary session: deletable, no reserved id, no flag, and nothing
    # downstream special-cases it. Its only distinction is existing.
    #
    # Best effort on purpose. Before a default agent is configured there
    # is nothing to bind to, and failing workspace creation over a
    # convenience would make the product unusable in exactly the window
    # where someone is setting it up.
    try:
        binding = await resolve_initial_binding(
            requested=None, storage_provider=storage_provider,
        )
    except ConfigError:
        logger.info(
            "create_workspace: no default agent configured, so workspace "
            "%s starts with no session", row.id,
        )
        return row
    except Exception:  # noqa: BLE001 - the workspace is the deliverable
        # A provider that cannot report system state is indistinguishable
        # from one with no default configured. Either way the workspace
        # is created and usable; only the convenience is skipped.
        logger.exception(
            "create_workspace: could not resolve a default binding for %s; "
            "starting with no session", row.id,
        )
        return row

    try:
        await create_session(
            workspace_id=row.id,
            binding=binding,
            initial_instructions=None,
            graph_input=None,
            auto_start=False,
            metadata=None,
            name="main",
            deps=SessionFactoryDeps(
                storage_provider=storage_provider,
                claim_engine=engine,
                scheduler=scheduler,
                workspace_registry=registry,
            ),
        )
    except Exception:  # noqa: BLE001 - the workspace exists and is usable
        logger.exception(
            "create_workspace: seeding the main session failed for %s",
            row.id,
        )
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
    summary="Pause a workspace (reserved - not implemented in v1)",
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
    summary="Resume a workspace (reserved - not implemented in v1)",
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
    (``session.json``) via :meth:`AgentSession.set_name` - the authoritative
    display source for the workspace sessions list - and best-effort mirrors
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
    except Exception as exc:  # noqa: BLE001 - advisory mirror, never fatal
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
    "/workspaces/{workspace_id}/sessions/{session_id}/compact",
    summary="Compact a session's history into a summary marker",
    responses=common_responses(404, 409, 422, 500),
)
async def compact_session_endpoint(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
    storage_provider=Depends(get_storage_provider),
    provider_registry=Depends(get_provider_registry),
    event_bus=Depends(get_event_bus),
) -> dict:
    """Summarise the visible history and append the fold marker.

    The summarising call takes seconds, so the session row is re-read
    afterwards: a concurrent write may have moved last_seq while the
    model was working, and the marker has to land after it.
    """
    from primer.agent.compaction import CompactionStrategy
    from primer.agent.compaction_mixin import force_compact
    from primer.agent.prompts import DEFAULT_COMPACTION_PROMPT
    from primer.model.agent import Agent
    from primer.model_profile import resolve_llm
    from primer.session.compaction import compact_session, guard_compactable
    from primer.workspace.session import reconstruct_compacted_history
    from primer.worker.io_shim import _WorkspaceIOShim

    sessions = storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(session_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    guard_compactable(row)  # 409: graph-bound, or a turn in flight

    # Snapshot-first, matching build_agent_executor: a frozen session
    # compacts under the definition it has been running with.
    agent = getattr(row.binding, "agent_snapshot", None)
    if agent is None:
        agent = await storage_provider.get_storage(Agent).get(
            row.binding.agent_id
        )
    if agent is None:
        raise NotFoundError(
            f"Agent {row.binding.agent_id!r} for session {session_id!r} "
            "no longer exists"
        )

    try:
        llm, llm_model = await resolve_llm(
            storage_provider,
            provider_registry,
            default_profile_id=agent.model.profile_id,
            override_profile_id=getattr(row.binding, "profile_id", None),
        )
    except (NotFoundError, ConfigError) as exc:
        raise ConfigError(
            f"Agent {agent.id!r} names model profile "
            f"{agent.model.profile_id!r}, which does not resolve to a "
            f"usable LLM: {exc}"
        ) from exc

    workspace = await registry.get_workspace(workspace_id)
    if workspace is None:
        raise NotFoundError(f"Workspace {workspace_id!r} is not available")
    state_path = getattr(workspace, "state_path", ".state")
    rel = f"{state_path}/sessions/{session_id}/messages.jsonl"
    try:
        raw = await workspace.read_file(rel)
    except Exception as exc:  # noqa: BLE001 - absent log is a 422, not a 5xx
        raise SemanticValidationError(
            "session has no recorded history to compact"
        ) from exc
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    history = reconstruct_compacted_history(text.splitlines())
    if not history:
        raise SemanticValidationError(
            "session has no visible history to compact"
        )

    prompt_field = getattr(agent, "compaction_prompt", None)
    compaction_prompt = (
        "\n\n".join(prompt_field) if prompt_field else DEFAULT_COMPACTION_PROMPT
    )

    async def _run(hist):
        return await force_compact(
            llm=llm,
            strategy=CompactionStrategy(),
            history=list(hist),
            compaction_prompt=compaction_prompt,
            # llm_model.model_name is None for an aggregated profile (see
            # ResolvedModel's docstring); it is also inert to
            # AggregatedLLM.stream/count_tokens (which resolve each
            # member's own model_name internally), but force_compact's
            # model_name is typed str and gets stored on a result shim
            # for display, so fall back to the profile id rather than
            # leak a raw None -- the same "no fabricated label, show the
            # profile's own id" fallback ruling (5) prescribes elsewhere.
            model_name=llm_model.model_name or llm_model.profile_id,
            context_length=llm_model.context_length,
        )

    io_shim = _WorkspaceIOShim(workspace_registry=registry)
    io_shim.register_session(session_id, workspace_id)
    fresh = await sessions.get(session_id) or row
    outcome = await compact_session(
        row=fresh, workspace_io=io_shim, history=history, run_compaction=_run,
    )
    await sessions.update(
        fresh.model_copy(update={"last_seq": outcome.compaction_marker_seq})
    )
    try:
        await event_bus.publish(
            f"session:{session_id}:tick",
            {"seq": outcome.compaction_marker_seq},
        )
    except Exception:  # noqa: BLE001 - the marker landed; the tick is a hint
        logger.exception("compaction tick publish failed for %s", session_id)
    return {
        "compaction_marker_seq": outcome.compaction_marker_seq,
        "summary": outcome.summary,
        "tokens_before": outcome.tokens_before,
        "tokens_after": outcome.tokens_after,
    }


class BindingSwitchBody(BaseModel):
    """Body of ``POST .../sessions/{sid}/binding``."""

    kind: Literal["agent", "graph"] = Field(
        ...,
        description="Which kind of target this session should run next.",
    )
    agent_id: str | None = Field(default=None)
    graph_id: str | None = Field(default=None)
    profile_id: str | None = Field(
        default=None,
        description=(
            "Optional ModelProfile override to apply with the switch, so "
            "target and model change in one gesture."
        ),
    )

    @model_validator(mode="after")
    def _target_matches_kind(self) -> "BindingSwitchBody":
        if self.kind == "agent" and not self.agent_id:
            raise ValueError("kind 'agent' requires agent_id")
        if self.kind == "graph" and not self.graph_id:
            raise ValueError("kind 'graph' requires graph_id")
        return self


@sessions_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/binding",
    response_model=WorkspaceSession,
    summary="Switch which agent or graph runs this session's next turn",
    responses=common_responses(404, 409, 422, 500),
)
async def switch_session_binding(
    body: BindingSwitchBody,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
    storage_provider=Depends(get_storage_provider),
) -> WorkspaceSession:
    """Point a session at a different agent or graph.

    An idle session switches immediately. A busy one queues the request
    and the drain checkpoint applies it before the next turn, so the
    running turn finishes under the binding it started with.
    """
    from primer.model.agent import Agent
    from primer.model.graph import Graph
    from primer.session.abandon import abandon_session_gate
    from primer.session.binding_switch import apply_binding_switch
    from primer.worker.io_shim import _WorkspaceIOShim

    sessions = storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(session_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    if row.status is SessionStatus.ENDED:
        raise ConflictError(
            f"session {session_id!r} has ended; reopen it before switching"
        )

    # Verified before anything is written: a switch to a target that
    # does not exist would strand the session on an unbuildable binding.
    if body.kind == "graph":
        target = await storage_provider.get_storage(Graph).get(body.graph_id)
        if target is None:
            raise NotFoundError(f"Graph {body.graph_id!r} does not exist")
    else:
        target = await storage_provider.get_storage(Agent).get(body.agent_id)
        if target is None:
            raise NotFoundError(f"Agent {body.agent_id!r} does not exist")

    request = {
        "kind": body.kind,
        "agent_id": body.agent_id,
        "graph_id": body.graph_id,
        "profile_id": body.profile_id,
        "actor": "user",
    }

    io_shim = _WorkspaceIOShim(workspace_registry=registry)
    io_shim.register_session(session_id, workspace_id)

    async def _resolve(_binding):
        return target

    if row.parked_status is not None:
        # A parked session waits on a human, and the gate belongs to the
        # OUTGOING agent. Queueing would leave the switch stuck behind a
        # gate only the agent being replaced can answer, which is the
        # deadlock switching exists to escape. Close the gate, then
        # switch, both under the lifecycle lock so a racing resume
        # cannot interleave between them.
        async with session_lifecycle_lock().acquire(session_id):
            fresh = await sessions.get(session_id)
            if fresh is None:
                raise NotFoundError(
                    f"Session {session_id!r} does not exist"
                )
            abandoned = await abandon_session_gate(
                sessions=sessions,
                workspace_io=io_shim,
                row=fresh,
                reason="binding switched",
            )
            return await apply_binding_switch(
                sessions=sessions,
                workspace_io=io_shim,
                row=abandoned,
                request=request,
                actor="user",
                resolve_snapshot=_resolve,
            )

    if row.turn_status in ("claimable", "running"):
        queued = row.model_copy(update={"pending_binding_switch": request})
        await sessions.update(queued)
        return queued

    return await apply_binding_switch(
        sessions=sessions,
        workspace_io=io_shim,
        row=row,
        request=request,
        actor="user",
        resolve_snapshot=_resolve,
    )


class ResponseFormatBody(BaseModel):
    """Body of ``PUT .../sessions/{sid}/response_format``."""

    response_format: dict[str, Any] | None = Field(
        default=None,
        description=(
            "JSON Schema to constrain this session's turns, or null to "
            "clear it and fall back to the agent default."
        ),
    )


@sessions_router.put(
    "/workspaces/{workspace_id}/sessions/{session_id}/response_format",
    response_model=WorkspaceSession,
    summary="Set or clear a session's persistent structured-output schema",
    responses=common_responses(404, 409, 422, 500),
)
async def set_session_response_format(
    body: ResponseFormatBody,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    storage_provider=Depends(get_storage_provider),
) -> WorkspaceSession:
    """Persist a schema for every later turn of this session.

    Refused mid-turn: the in-flight turn already resolved its format,
    so accepting would suggest an effect this call cannot have.
    """
    sessions = storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(session_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    if row.turn_status != "idle":
        raise ConflictError(
            "session has a turn in flight; response_format applies from "
            "the next turn, so set it once the current one finishes"
        )
    # model_copy skips validation, so the schema is re-validated here and
    # the pydantic error converted: raw, it escapes as a 500 instead of
    # the 422 an invalid schema deserves.
    updated = row.model_copy(update={"response_format": body.response_format})
    try:
        WorkspaceSession.model_validate(updated.model_dump(mode="json"))
    except PydanticValidationError as exc:
        raise SemanticValidationError(
            f"response_format is not a valid JSON Schema: {exc}"
        ) from exc
    await sessions.update(updated)
    return updated


class RewindBody(BaseModel):
    """Body of ``POST /v1/workspaces/{id}/sessions/{sid}/rewind``."""

    to_seq: int = Field(
        ...,
        ge=1,
        description=(
            "Seq of the user_input record to keep. Every visible record "
            "after it is dropped from the reconstructed history; nothing "
            "is deleted from the log."
        ),
    )


@sessions_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/rewind",
    summary="Rewind a session's visible history to a kept user_input",
    responses=common_responses(404, 409, 422, 500),
)
async def rewind_session(
    body: RewindBody,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Cut a session's visible history back to an earlier user message.

    Appends a rewind marker; nothing is deleted. The read-time replay
    walk drops what follows, so the cut is auditable and the log stays
    append-only.

    Rejects unless the session is fully idle: rewinding under a running
    or parked turn would race the writer that turn is still using, and
    the seq the marker names could move underneath it.
    """
    from primer.session.rewind import append_rewind_marker, check_rewind_target
    from primer.worker.io_shim import _WorkspaceIOShim

    sessions = storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(session_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    if row.turn_status != "idle" or row.parked_status is not None:
        raise ConflictError(
            "session is not idle; rewind requires no turn in flight"
        )

    workspace = await registry.get_workspace(workspace_id)
    if workspace is None:
        raise NotFoundError(f"Workspace {workspace_id!r} is not available")
    state_path = getattr(workspace, "state_path", ".state")
    rel = f"{state_path}/sessions/{session_id}/messages.jsonl"
    try:
        raw = await workspace.read_file(rel)
    except Exception as exc:  # noqa: BLE001 - absent log is a 422, not a 5xx
        raise SemanticValidationError(
            "session has no recorded history to rewind"
        ) from exc
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    lines = text.splitlines()

    # Raises ConflictError for the compaction case (amendment C2) and
    # ValidationError for a malformed target; both before any write.
    check_rewind_target(lines, to_seq=body.to_seq)

    io_shim = _WorkspaceIOShim(workspace_registry=registry)
    io_shim.register_session(session_id, workspace_id)
    marker_seq = await append_rewind_marker(
        workspace_io=io_shim,
        session_id=session_id,
        start_seq=row.last_seq,
        to_seq=body.to_seq,
        actor="user",
    )
    await sessions.update(row.model_copy(update={"last_seq": marker_seq}))
    return {
        "session_id": session_id,
        "to_seq": body.to_seq,
        "marker_seq": marker_seq,
    }


@sessions_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/steer",
    response_model=WorkspaceSession,
    summary="Send a message - invoke / steer / resume (auto-wake)",
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
    artifact_registry=Depends(get_optional_artifact_storage_registry),
) -> WorkspaceSession:
    """Send a user message to a session and auto-wake it.

    One input, four behaviours (studio-agents-interact §5.1): a message to
    a CREATED session invokes it; to a RUNNING/WAITING session it queues as
    the next turn (steer); to a PAUSED session it resumes; to an ENDED
    session it reopens it as a fresh invocation (divider + run). 409 only
    when an ENDED session is non-restartable (workspace_lost/force_deleted).
    """
    from primer.model.external_tool import ExternalToolCall
    from primer.session.enqueue import SessionWakeDeps, wake_session
    from primer.session.external_tools import (
        CANCEL_REASON_SUPERSEDED,
        _pending_targets,
        apply_tool_results,
        cancel_pending_external,
    )
    from primer.session.pending_messages import store_pending_steer
    from primer.session.steer_routing import ROUTE_PENDING, route_steer
    from primer.session.yields import durably_wake_session
    from primer.worker.yield_runtime import make_cancelled_payload

    sessions = storage_provider.get_storage(WorkspaceSession)
    call_storage = storage_provider.get_storage(ExternalToolCall)
    row = await sessions.get(session_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )

    # Registration gate: external_tools requires allow_external_tools on
    # the session's agent (snapshot when frozen, else the live row).
    if body.external_tools and not await _binding_allows_external(
        row, storage_provider
    ):
        raise SemanticValidationError(
            "this session's agent does not have allow_external_tools "
            "enabled; external_tools rejected"
        )

    # The steer is accepted for delivery from here on (remaining
    # branches route it, they don't reject it).
    from primer.events.recorder import recorder_for
    await recorder_for(storage_provider, event_bus).emit(
        "session.steered",
        workspace_id=workspace_id,
        session_id=session_id,
        payload={
            "has_instruction": bool(body.instruction),
            "has_tool_results": bool(body.tool_results),
            "has_attachments": bool(body.attachments),
        },
    )

    # Dispatch rule (external-tools spec §6), in order:
    # 1+2. Validate then apply tool_results (409-atomic inside the helper).
    if body.tool_results:
        await apply_tool_results(
            row,
            body.tool_results,
            call_storage=call_storage,
            session_storage=sessions,
            engine=engine,
            event_bus=event_bus,
            storage_provider=storage_provider,
        )
        row = await sessions.get(session_id)  # refreshed park state

    # 3. Message content cancels every still-pending external call, waking
    #    the park with the synthetic cancelled payload so the turn resumes
    #    and pairs the call before the queued instruction is consumed.
    if body.instruction:
        cancelled = await cancel_pending_external(
            call_storage=call_storage, session_id=session_id,
        )
        if cancelled and row is not None:
            payload = make_cancelled_payload(reason=CANCEL_REASON_SUPERSEDED)
            for _tcid, key in _pending_targets(row).items():
                await durably_wake_session(
                    row,
                    event_key=key,
                    payload=payload,
                    session_storage=sessions,
                    engine=engine,
                )
                try:
                    await event_bus.publish(key, payload)
                except Exception:  # noqa: BLE001 - durable flip landed
                    logger.exception(
                        "external tool cancel publish failed for %r", key
                    )

        # A session runs one turn at a time, so an instruction that
        # arrives while a turn is still open is queued rather than
        # written as a second user message, which would break the
        # 1:1 user-message-to-terminal pairing the drain counts. The
        # drain realizes it at the next checkpoint. Results-carrying
        # bodies are exempt: those resume the open turn on purpose, and
        # the instruction is meant to steer that same resumed turn.
        # Bodies carrying tool defs are exempt alongside results: a
        # pending row has nowhere to hold external_tools, so deferring
        # one would silently drop the registration for the turn it was
        # meant to arm. Bodies carrying attachments are exempt for the
        # same reason: PendingSessionMessage.parts is a plain text-only
        # projection today (realize_next_pending only ever extracts
        # type=="text"), so a deferred attachment would silently vanish
        # rather than reach the model. append_instruction's own message
        # FIFO (used below via wake_session) already carries parts.
        if (
            row is not None
            and not body.tool_results
            and not body.external_tools
            and not body.attachments
            and route_steer(row) == ROUTE_PENDING
        ):
            await store_pending_steer(
                storage_provider=storage_provider,
                session_id=session_id,
                text=body.instruction,
            )
            return await sessions.get(session_id)

        # Stamp the per-turn schema where the dispatch pops it. Written
        # before the wake so the turn it arms is the one that sees it.
        if body.response_format is not None and row is not None:
            from primer.session.response_format import EPHEMERAL_KEY

            meta = dict(row.metadata or {})
            meta[EPHEMERAL_KEY] = body.response_format
            row = row.model_copy(update={"metadata": meta})
            await sessions.update(row)

        # Resolve attachments to artifact-backed Parts through the SAME
        # pipeline ask_user/inform_user already use for outbound files
        # (primer.channel.media.media_from_workspace_files). Best-effort,
        # matching that pipeline's own tolerance: a missing/oversized/
        # disallowed file or an unconfigured artifact store drops the
        # attachment with a log rather than failing the whole steer.
        extra_parts = None
        extra_payload = None
        if body.attachments:
            from primer.channel.media import media_from_workspace_files

            if artifact_registry is None:
                logger.warning(
                    "steer_session: attachments given but no artifact "
                    "storage registry is configured; dropping %d "
                    "attachment(s) for session %r",
                    len(body.attachments), session_id,
                )
            else:
                workspace = await registry.get_workspace(workspace_id)
                artifact_store = await artifact_registry.get_default()
                extra_parts = await media_from_workspace_files(
                    workspace, artifact_store,
                    [a.path for a in body.attachments],
                )
                if extra_parts:
                    extra_payload = {
                        "attachments": [a.path for a in body.attachments],
                    }

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
            external_tools=body.external_tools,
            extra_parts=extra_parts,
            extra_payload=extra_payload,
            deps=deps,
        )

    # 4. Pure-results body: the park is resumable; no new turn to trigger.
    return await sessions.get(session_id)


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
    from primer.model.external_tool import ExternalToolCall
    from primer.session.enqueue import SessionWakeDeps
    from primer.session.external_tools import cancel_pending_external
    from primer.session.reset import SessionResetDeps, restart_session

    # A restart discards the previous run's in-flight state; resolve any
    # audit rows a prior park left pending (a session that ENDED via
    # failure never went through the cancel route's sweep).
    await cancel_pending_external(
        call_storage=storage_provider.get_storage(ExternalToolCall),
        session_id=session_id,
        reason="session restarted",
    )

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


class SessionAttachBody(BaseModel):
    """Body of ``POST /v1/workspaces/{id}/sessions/{sid}/attach``."""

    client_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Opaque per-tab client identifier. Re-posting with the same id "
            "is the heartbeat: it extends the TTL and leaves the "
            "attach-time high-water mark untouched."
        ),
    )


@sessions_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/attach",
    summary="Attach a client to a session (also the heartbeat)",
    responses=common_responses(404, 422, 500),
)
async def attach_session(
    body: SessionAttachBody = Body(...),
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Register (or refresh) a live client attachment for this session.

    Turns STARTED while an attachment is live carry the client toolset
    (S3 spec section 4). ``attached_seq`` is the replay fence the caller
    must apply to delivery records: execute above it, render at or below.
    """
    from primer.model.client_attachment import ClientAttachment
    from primer.session.attachment import ATTACH_TTL_SECONDS, attach_or_refresh

    sessions = storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(session_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    att = await attach_or_refresh(
        storage_provider.get_storage(ClientAttachment),
        workspace_id=workspace_id,
        session_id=session_id,
        client_id=body.client_id,
        last_seq=row.last_seq,
    )
    return {
        "client_id": att.client_id,
        "attached_seq": att.attached_seq,
        "expires_at": att.expires_at.isoformat(),
        "ttl_seconds": ATTACH_TTL_SECONDS,
    }


@sessions_router.delete(
    "/workspaces/{workspace_id}/sessions/{session_id}/attach",
    summary="Detach a client from a session",
    responses=common_responses(404, 422, 500),
)
async def detach_session(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    client_id: str = Query(..., min_length=1),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Best-effort detach. The TTL covers a client that never calls it."""
    from primer.model.client_attachment import ClientAttachment
    from primer.session.attachment import detach

    sessions = storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(session_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    removed = await detach(
        storage_provider.get_storage(ClientAttachment),
        session_id=session_id,
        client_id=client_id,
    )
    return {"detached": removed}


@sessions_router.delete(
    "/workspaces/{workspace_id}/sessions/{session_id}"
    "/pending_messages/{pending_id}",
    status_code=204,
    summary="Dismiss one queued follow-up steer",
    responses=common_responses(404, 500),
)
async def dismiss_pending_message(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    pending_id: str = Path(...),
    storage_provider=Depends(get_storage_provider),
) -> None:
    """Drop a queued steer before a turn realizes it.

    The console has offered this dismiss since the flag day (a failed
    turn otherwise leaves the queued follow-up parked forever), but the
    route never existed: the X on a queued chip threw client-side and
    the row was undismissable (BDD round 2, 2026-08-24).
    """
    from primer.model.workspace_session import PendingSessionMessage

    sessions = storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(session_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    storage = storage_provider.get_storage(PendingSessionMessage)
    pending = await storage.get(pending_id)
    if pending is None or pending.session_id != session_id:
        raise NotFoundError(
            f"Pending message {pending_id!r} does not exist on session "
            f"{session_id!r}"
        )
    await storage.delete(pending_id)


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
    # No origin decoration: with collection mounting retired, every entry
    # is workspace-native and nothing is collection-backed.
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
                "origin": entry.origin,
            }
        )
    items.sort(key=lambda x: (0 if x["is_dir"] else 1, x["name"]))
    return {"path": path, "items": items}


_MAX_RECURSIVE_WALK_ENTRIES = 10_000


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
    # No origin decoration: with collection mounting retired, every
    # entry is workspace-native and nothing is collection-backed.
    #
    # 01a0644b: recursive=True used to walk the ENTIRE subtree before
    # this route got a chance to slice it - expensive on a large
    # workspace (e.g. a vendored node_modules) regardless of how small
    # a page the caller actually asked for. max_entries bounds the walk
    # itself to just enough to satisfy this page (capped so a huge
    # offset can't still trigger an unbounded walk). This makes the
    # already-supported recursive=True finally safe to use for what it
    # was added for (e.g. the palette's cross-directory recent-files
    # group), no new endpoint or query param needed.
    #
    # Trade-off: when a tree has more entries than the walk cap, the
    # page is a bounded sample in filesystem traversal order (then
    # sorted among itself), not a mathematically exact slice of the
    # tree's true global alphabetical ordering - unavoidable without
    # walking (and therefore paying for) the whole tree regardless of
    # page size. Fine for "browse/recent", not a fit for bulk export
    # (download_archive already exists for that).
    max_entries = (
        min(offset + limit, _MAX_RECURSIVE_WALK_ENTRIES) if recursive else None
    )
    entries = await ws.list_files(path, recursive=recursive, max_entries=max_entries)
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
        except Exception as exc:  # noqa: BLE001 - base64.binascii.Error
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
                except Exception:  # noqa: BLE001 - ignore malformed header
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
    except Exception:  # noqa: BLE001 - wake is best-effort
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
    with_files: bool = Query(
        default=False,
        description=(
            "Include per-file line deltas on each commit's `files`. Off by "
            "default because it widens the response; on the local backend it "
            "costs no extra git process (--numstat rides the same git log). "
            "Backends that cannot supply file data leave `files` null rather "
            "than empty -- null means unknown, not zero."
        ),
    ),
    registry: WorkspaceRegistry = Depends(get_workspace_registry),
) -> dict:
    ws = await registry.get_workspace(workspace_id)
    commits = await ws.log(limit=limit, with_files=with_files)
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
                    # Who may decide (P6 approver routing); None = anyone.
                    "approvers": metadata.get("approvers"),
                    # The literal gated call, so the decision card can
                    # show the command being judged (design section 8:
                    # never a free-text question). BDD pass 2026-08-24.
                    "resume_metadata": {
                        "original_call": metadata.get("original_call"),
                        # UX reconcile wave 5: an ask_user park stamps this
                        # at yield time (_system_tools.py) and the
                        # dedicated ask_user/pending route already returns
                        # it (AskUserPendingResponse.response_schema) -
                        # this route dropped it, so a card built from the
                        # aggregate list could never offer discrete answer
                        # options, only free text.
                        "response_schema": metadata.get("response_schema"),
                    },
                }
            )
        if len(resp.items) < page_size:
            break
        offset += page_size

    return {"items": items}


@yields_pending_router.get(
    "/yields/pending",
    summary="Aggregated pending attention across all workspaces (Inbox / System dashboard)",
    responses=common_responses(500),
)
async def list_pending_attention(
    workspace_id: str | None = Query(
        default=None, description="Optional: restrict to a single workspace"
    ),
    limit: int = Query(100, ge=1, le=500),
    session_storage=Depends(get_session_storage),
    workspace_storage=Depends(get_workspace_storage),
) -> dict:
    """Aggregate pending attention (approvals, ask_user, and everything else
    parked on a human) across every workspace the caller can see.

    Drives the cross-workspace Inbox rail and the System dashboard's
    "needs a human, every workspace" table. Unlike
    ``GET /workspaces/{workspace_id}/yields/pending`` (session-shaped,
    single-workspace), this endpoint is workspace-shaped for the Inbox row
    contract::

        {
            "items": [
                {
                    "workspace_id": str,
                    "workspace_name": str | None,
                    "session_id": str,
                    "session_name": str | None,
                    "kind": "approval" | "ask" | "parked",
                    "agent_binding": dict,
                    "created_at": str,   # ISO-8601; parked_at, falling back
                                         # to the session's created_at
                },
                …
            ],
            "total": int,
        }

    ``kind`` collapses the full yield-tool vocabulary down to the three
    buckets the Inbox cares about: ``"approval"`` (tool-approval gate),
    ``"ask"`` (``ask_user``), and ``"parked"`` (everything else parked
    pending a human -- ``watch_files``, ``sleep``, etc).

    Only sessions with ``parked_status == "parked"`` are included. Items are
    ordered newest-first by the same timestamp exposed as ``created_at`` and
    capped at ``limit`` (default 100); ``total`` reports the full matching
    count regardless of the cap. No per-workspace ACL exists today (any
    signed-in user may see any workspace), matching this router's other
    routes.
    """
    from primer.model.storage import OffsetPage
    from primer.storage.q import Q
    from primer.model.workspace_session import WorkspaceSession

    predicate_q = Q(WorkspaceSession).where("parked_status", "parked")
    if workspace_id is not None:
        predicate_q = predicate_q.where("workspace_id", workspace_id)
    predicate = predicate_q.build()

    sessions: list[WorkspaceSession] = []
    offset = 0
    page_size = 200
    while True:
        resp = await session_storage.find(
            predicate,
            OffsetPage(offset=offset, length=page_size),
        )
        sessions.extend(resp.items)
        if len(resp.items) < page_size:
            break
        offset += page_size

    workspace_names: dict[str, str | None] = {}

    async def _workspace_name(wid: str) -> str | None:
        if wid not in workspace_names:
            row = await workspace_storage.get(wid)
            workspace_names[wid] = row.name if row is not None else None
        return workspace_names[wid]

    rows = []
    for sess in sessions:
        blob: dict = sess.parked_state or {}
        yielded_blob: dict = blob.get("yielded") or {}
        tool_name: str = yielded_blob.get("tool_name") or ""

        if tool_name == "_approval":
            kind = "approval"
        elif tool_name == "ask_user":
            kind = "ask"
        else:
            kind = "parked"

        created_at = sess.parked_at if sess.parked_at is not None else sess.created_at

        rows.append(
            (
                created_at,
                {
                    "workspace_id": sess.workspace_id,
                    "workspace_name": await _workspace_name(sess.workspace_id),
                    "session_id": sess.id,
                    "session_name": sess.name,
                    "kind": kind,
                    "agent_binding": sess.binding.model_dump(mode="json"),
                    "created_at": created_at.isoformat(),
                },
            )
        )

    rows.sort(key=lambda row: row[0], reverse=True)
    items = [row[1] for row in rows]

    return {"items": items[:limit], "total": len(items)}


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

    01a06c94: a graph superstep can park on SEVERAL nodes at once (e.g. a
    fan-out with concurrent approval gates) - this is the console's actual
    DecisionCard data source (nv-session-doc.jsx's `gates` resource), so
    every pending entry is returned now via the shared
    ``primer.session.pending_gates`` resolver, not just the primary one
    the old single-``yielded``-blob read projected. Non-primary gates were
    previously invisible here and therefore unanswerable in the console.
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
        for gate in enumerate_pending_gates(blob):
            tool_name = gate["kind"]
            metadata: dict = gate["resume_metadata"]
            items.append({
                "session_id": sess.id,
                "kind": _extract_yield_kind(tool_name),
                "prompt": _extract_yield_prompt(tool_name, metadata),
                "tool_call_id": gate["tool_call_id"],
                "parked_at": (
                    sess.parked_at.isoformat()
                    if sess.parked_at is not None else None
                ),
                # Who may decide (P6 approver routing); None = anyone.
                "approvers": metadata.get("approvers"),
                # The literal gated call (see the aggregated route above).
                "resume_metadata": {
                    "original_call": metadata.get("original_call"),
                    # UX reconcile wave 5 - see the aggregated route above.
                    "response_schema": metadata.get("response_schema"),
                },
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
    1:1 with live tap frames - the same reader (:func:`read_session_since`) that
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
