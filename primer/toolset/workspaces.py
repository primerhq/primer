"""``workspaces`` reserved internal toolset — dogfoods the workspace API.

Always available (built once at app startup, like ``system``). The
internal collections subsystem ingests its tools into the
``_internal_tools`` collection during bootstrap so agents can search
for them.

Tool catalog (25 tools)
-----------------------

Provider (CRUD minus update):
    list_workspace_providers, get_workspace_provider,
    create_workspace_provider, delete_workspace_provider

Template (full CRUD):
    list_workspace_templates, get_workspace_template,
    create_workspace_template, update_workspace_template,
    delete_workspace_template

Workspace (CRUD minus update):
    list_workspaces, get_workspace, create_workspace, delete_workspace

Sessions:
    list_workspace_sessions, get_workspace_session,
    pause_workspace_session, resume_workspace_session,
    steer_workspace_session

Files:
    list_workspace_files, get_workspace_file_info,
    read_workspace_file, delete_workspace_file, write_workspace_file

Log:
    get_workspace_log

Yielding (M4):
    watch_files
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import Tool, ToolCallResult
from primer.model.except_ import (
    BadRequestError,
    ConflictError,
    MatrixError,
    NotFoundError,
)
from primer.model.storage import CursorPage, OffsetPage, OrderBy
from primer.model.workspace import (
    Workspace as WorkspaceRow,
    WorkspaceProvider,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset.internal import InternalToolsetProvider, ToolHandler


if TYPE_CHECKING:
    from primer.api.registries import WorkspaceRegistry
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


WORKSPACES_TOOLSET_ID = "workspaces"


# ===========================================================================
# JSON / error helpers
# ===========================================================================


def _to_json(payload: Any) -> str:
    if isinstance(payload, BaseModel):
        return payload.model_dump_json()
    if isinstance(payload, list):
        return json.dumps(
            [
                p.model_dump(mode="json") if isinstance(p, BaseModel) else p
                for p in payload
            ],
            default=str,
        )
    return json.dumps(payload, default=str)


def _ok(payload: Any) -> ToolCallResult:
    return ToolCallResult(output=_to_json(payload), is_error=False)


def _err(message: str, *, error_type: str = "tool-error") -> ToolCallResult:
    return ToolCallResult(
        output=json.dumps({"type": error_type, "message": message}),
        is_error=True,
    )


def _err_from_validation(exc: ValidationError) -> ToolCallResult:
    return _err(
        "argument validation failed: " + json.dumps(exc.errors(), default=str),
        error_type="validation-error",
    )


def _err_from_matrix(exc: MatrixError, *, error_type: str) -> ToolCallResult:
    return _err(getattr(exc, "message", str(exc)), error_type=error_type)


# ===========================================================================
# Argument models
# ===========================================================================


class _PaginationArgs(BaseModel):
    limit: int = Field(default=20, ge=1, le=200)
    offset: int | None = Field(default=None, ge=0)
    cursor: str | None = Field(default=None)
    order_by: list[str] | None = Field(default=None)


class _IdArgs(BaseModel):
    id: str = Field(..., min_length=1, description="Entity id.")


class _CreateProviderArgs(BaseModel):
    entity: WorkspaceProvider = Field(..., description="WorkspaceProvider body.")


class _CreateTemplateArgs(BaseModel):
    entity: WorkspaceTemplate = Field(..., description="WorkspaceTemplate body.")


class _UpdateTemplateArgs(BaseModel):
    id: str = Field(..., min_length=1)
    entity: WorkspaceTemplate = Field(...)


class _CreateWorkspaceArgs(BaseModel):
    id: str | None = Field(default=None)
    template_id: str = Field(..., min_length=1)
    overrides: WorkspaceTemplateOverrides | None = Field(default=None)


class _WorkspaceSessionArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)


class _SteerArgs(_WorkspaceSessionArgs):
    instruction: str = Field(..., min_length=1)


class _WorkspaceListSessionsArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class _WorkspacePathArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class _ListFilesArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    path: str = Field(default=".")
    recursive: bool = Field(default=False)
    limit: int = Field(default=200, ge=1, le=2000)
    offset: int = Field(default=0, ge=0)


class _ReadFileArgs(_WorkspacePathArgs):
    encoding: str = Field(
        default="text",
        description="``text`` (UTF-8 decode) or ``base64`` (binary as base64).",
    )


class _WriteFileArgs(_WorkspacePathArgs):
    content: str = Field(..., description="File content; encoding selects interpretation.")
    encoding: str = Field(
        default="text",
        description="``text`` (UTF-8 encoded as-is) or ``base64`` (decoded to raw bytes).",
    )


class _LogArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    limit: int = Field(default=50, ge=1, le=500)


# ===========================================================================
# Pagination helpers
# ===========================================================================


def _parse_page(args: _PaginationArgs) -> OffsetPage | CursorPage:
    if args.offset is not None and args.cursor is not None:
        raise ValueError("supply either ``offset`` or ``cursor``, not both")
    if args.cursor is not None:
        return CursorPage(cursor=args.cursor, length=args.limit)
    return OffsetPage(offset=args.offset or 0, length=args.limit)


def _parse_order_by(spec: list[str] | None) -> list[OrderBy] | None:
    if spec is None:
        return None
    parsed: list[OrderBy] = []
    for entry in spec:
        if ":" in entry:
            field, direction = entry.split(":", 1)
            direction = direction.strip().lower() or "asc"
            if direction not in ("asc", "desc"):
                raise ValueError(
                    f"invalid order_by direction {direction!r} in {entry!r}"
                )
        else:
            field, direction = entry, "asc"
        parsed.append(OrderBy(field=field.strip(), direction=direction))  # type: ignore[arg-type]
    return parsed


# ===========================================================================
# Generic CRUD helpers
# ===========================================================================


_OnMutate = Callable[[str], Awaitable[None]] | None


def _make_list_handler(storage_factory: Callable[[], Any]) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _PaginationArgs.model_validate(arguments)
            page = _parse_page(args)
            order_by = _parse_order_by(args.order_by)
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                return _err_from_validation(exc)
            return _err(str(exc), error_type="bad-request")
        return _ok(await storage_factory().list(page, order_by=order_by))

    return _handler


def _make_get_handler(
    storage_factory: Callable[[], Any], cls_name: str
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        row = await storage_factory().get(args.id)
        if row is None:
            return _err(
                f"{cls_name} {args.id!r} does not exist", error_type="not-found"
            )
        return _ok(row)

    return _handler


def _make_create_handler(
    args_cls: type[BaseModel],
    storage_factory: Callable[[], Any],
    cls_name: str,
    on_create: _OnMutate = None,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = args_cls.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        entity = args.entity  # type: ignore[attr-defined]
        storage = storage_factory()
        if await storage.get(entity.id) is not None:
            return _err(
                f"{cls_name} with id {entity.id!r} already exists",
                error_type="conflict",
            )
        try:
            created = await storage.create(entity)
        except (ConflictError, MatrixError) as exc:
            return _err_from_matrix(exc, error_type="storage-error")
        if on_create is not None:
            await on_create(created.id)
        return _ok(created)

    return _handler


def _make_update_handler(
    args_cls: type[BaseModel],
    storage_factory: Callable[[], Any],
    cls_name: str,
    on_update: _OnMutate = None,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = args_cls.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        entity = args.entity  # type: ignore[attr-defined]
        if entity.id != args.id:  # type: ignore[attr-defined]
            return _err(
                f"path id {args.id!r} does not match body id {entity.id!r}",  # type: ignore[attr-defined]
                error_type="conflict",
            )
        storage = storage_factory()
        if await storage.get(args.id) is None:  # type: ignore[attr-defined]
            return _err(
                f"{cls_name} {args.id!r} does not exist", error_type="not-found"  # type: ignore[attr-defined]
            )
        try:
            updated = await storage.update(entity)
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="storage-error")
        if on_update is not None:
            await on_update(updated.id)
        return _ok(updated)

    return _handler


def _make_delete_handler(
    storage_factory: Callable[[], Any],
    cls_name: str,
    on_delete: _OnMutate = None,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        storage = storage_factory()
        if await storage.get(args.id) is None:
            return _err(
                f"{cls_name} {args.id!r} does not exist", error_type="not-found"
            )
        try:
            await storage.delete(args.id)
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="storage-error")
        if on_delete is not None:
            await on_delete(args.id)
        return _ok({"deleted": True, "id": args.id})

    return _handler


def _tool(
    name: str,
    description: str,
    args_cls: type[BaseModel],
    handler: ToolHandler,
) -> tuple[str, tuple[Tool, ToolHandler]]:
    return name, (
        Tool(
            id=name,
            description=description,
            toolset_id=WORKSPACES_TOOLSET_ID,
            args_schema=args_cls.model_json_schema(),
        ),
        handler,
    )


# ===========================================================================
# watch_files — third yielding tool (M4 of the yielding-tools feature).
# See docs/superpowers/specs/2026-05-22-yielding-tools-design.md §8.3.
#
# Pauses the agent's turn until one of the watched paths changes on
# disk. The matching background watcher (matrix/bus/watcher.py) polls
# mtimes and publishes a coalesced change burst on the event bus.
# ===========================================================================


class _WatchFilesArgs(BaseModel):
    """Workspace-relative paths to watch + polling/coalescing controls."""

    paths: list[str] = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "Workspace-relative paths to watch. Files and directories "
            "are both accepted; directories are watched recursively "
            "(at one level — child file mtime changes count). Paths "
            "must NOT be absolute and must NOT contain ``..`` segments "
            "(no traversal). Required, at least one path."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Optional per-call timeout. When omitted, falls back to "
            "the global yield cap. If no change fires before the "
            "deadline, the resume hook returns "
            "``{timed_out: true, changes: [], elapsed_seconds: ...}``."
        ),
    )
    batch_window_ms: int = Field(
        default=250,
        ge=0,
        le=5000,
        description=(
            "After the first change is detected, the watcher waits "
            "this many milliseconds for more changes before publishing "
            "one coalesced burst. Defaults to 250ms — tune up for "
            "noisy file systems (long compile, large directory tree)."
        ),
    )


def watch_files_resume(
    yield_metadata: dict[str, Any],
    event_payload: Any,
) -> ToolCallResult:
    """Resume hook for watch_files — translate payload into tool result.

    Three branches:

    * real changes (``{"changes": [...]}`` from the watcher) →
      ``{"timed_out": false, "changes": [...]}``
    * :class:`YieldTimeout` from the sweeper → ``{"timed_out": true,
      "changes": [], "elapsed_seconds": ...}``
    * :class:`YieldCancelled` from the cancel-yielded-tool API →
      ``{"cancelled": true, "reason": ..., "changes": []}``
    """
    from primer.model.yield_ import YieldCancelled, YieldTimeout  # avoid cycle

    if isinstance(event_payload, YieldTimeout):
        return _ok(
            {
                "timed_out": True,
                "changes": [],
                "elapsed_seconds": event_payload.elapsed_seconds,
            }
        )
    if isinstance(event_payload, YieldCancelled):
        return _ok(
            {
                "cancelled": True,
                "reason": event_payload.reason,
                "changes": [],
                "elapsed_seconds": event_payload.elapsed_seconds,
            }
        )
    changes = (
        event_payload.get("changes", [])
        if isinstance(event_payload, dict)
        else []
    )
    return _ok({"timed_out": False, "changes": list(changes)})


async def _watch_files_handler(
    arguments: dict[str, Any],
    *,
    ctx: ToolContext,
) -> ToolCallResult | Yielded:
    try:
        args = _WatchFilesArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)
    if ctx.session_id is None:
        return _err(
            "watch_files requires ctx.session_id; the worker must pass "
            "the live session id when invoking yielding tools",
            error_type="bad-request",
        )
    if ctx.workspace_id is None:
        return _err(
            "watch_files requires ctx.workspace_id; the watcher needs "
            "the workspace's filesystem root to resolve the paths",
            error_type="bad-request",
        )
    # Path hygiene: absolute paths and traversal segments could let
    # the agent escape the workspace sandbox. Reject upfront so the
    # watcher never has to defend against them.
    for p in args.paths:
        if p.startswith("/") or (len(p) >= 2 and p[1] == ":"):
            return _err(
                f"absolute paths not allowed: {p!r}",
                error_type="bad-request",
            )
        # Split on both POSIX and Windows separators so the check
        # works regardless of the host OS the agent runs on.
        segs = p.replace("\\", "/").split("/")
        if ".." in segs:
            return _err(
                f"path traversal not allowed: {p!r}",
                error_type="bad-request",
            )

    return Yielded(
        tool_name="",  # provider stamps
        event_key=f"watch:{ctx.session_id}:{ctx.tool_call_id}",
        timeout=args.timeout_seconds,
        resume_metadata={
            "paths": list(args.paths),
            "batch_window_ms": args.batch_window_ms,
            "workspace_id": ctx.workspace_id,
            "tool_call_id": ctx.tool_call_id,
            "registered_at_iso": datetime.now(timezone.utc).isoformat(),
        },
    )


# ===========================================================================
# Build the toolset
# ===========================================================================


def build_workspaces_toolset(
    *,
    storage_provider: "StorageProvider",
    workspace_registry: "WorkspaceRegistry",
    toolset_id: str = WORKSPACES_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the immutable ``_workspaces`` toolset."""
    registry: dict[str, tuple[Tool, ToolHandler]] = {}

    def _provider_storage():
        return storage_provider.get_storage(WorkspaceProvider)

    def _template_storage():
        return storage_provider.get_storage(WorkspaceTemplate)

    def _workspace_storage():
        return storage_provider.get_storage(WorkspaceRow)

    async def _inv_provider(eid: str) -> None:
        await workspace_registry.invalidate(eid)

    # ------------------- Provider CRUD (no update) ---------------------
    name, entry = _tool(
        "list_workspace_providers",
        (
            "List configured WorkspaceProviders with pagination. Same "
            "limit/offset/cursor/order_by contract as the rest of the "
            "system toolset's list_* tools. Returns ``items``, "
            "``length``, ``total`` (offset mode), ``next_cursor`` "
            "(cursor mode)."
        ),
        _PaginationArgs,
        _make_list_handler(_provider_storage),
    )
    registry[name] = entry
    name, entry = _tool(
        "get_workspace_provider",
        (
            "Fetch one WorkspaceProvider by id. Returns the provider "
            "row with its discriminated config. ``type=not-found`` "
            "when missing."
        ),
        _IdArgs,
        _make_get_handler(_provider_storage, "WorkspaceProvider"),
    )
    registry[name] = entry
    name, entry = _tool(
        "create_workspace_provider",
        (
            "Create a new WorkspaceProvider. Body shape is the full "
            "WorkspaceProvider schema with ``provider`` discriminator "
            "and matching ``config``. The Update operation is "
            "intentionally absent — to change a provider's config, "
            "delete and recreate."
        ),
        _CreateProviderArgs,
        _make_create_handler(
            _CreateProviderArgs, _provider_storage, "WorkspaceProvider"
        ),
    )
    registry[name] = entry
    name, entry = _tool(
        "delete_workspace_provider",
        (
            "Delete a WorkspaceProvider by id. Cascades to drop the "
            "cached backend instance from the WorkspaceRegistry."
        ),
        _IdArgs,
        _make_delete_handler(
            _provider_storage, "WorkspaceProvider", on_delete=_inv_provider
        ),
    )
    registry[name] = entry

    # ------------------- Template CRUD (full) -------------------------
    name, entry = _tool(
        "list_workspace_templates",
        "List WorkspaceTemplates with pagination.",
        _PaginationArgs,
        _make_list_handler(_template_storage),
    )
    registry[name] = entry
    name, entry = _tool(
        "get_workspace_template",
        "Fetch one WorkspaceTemplate by id; ``type=not-found`` if missing.",
        _IdArgs,
        _make_get_handler(_template_storage, "WorkspaceTemplate"),
    )
    registry[name] = entry
    name, entry = _tool(
        "create_workspace_template",
        (
            "Create a new WorkspaceTemplate. Body must reference an "
            "existing ``provider_id`` and include the materialisation "
            "recipe (packages, files, env, init_commands, resources)."
        ),
        _CreateTemplateArgs,
        _make_create_handler(
            _CreateTemplateArgs, _template_storage, "WorkspaceTemplate"
        ),
    )
    registry[name] = entry
    name, entry = _tool(
        "update_workspace_template",
        (
            "Replace an existing WorkspaceTemplate. The body's ``id`` "
            "must equal the path ``id``. Existing materialised "
            "Workspaces are NOT re-materialised; only future creates "
            "see the new recipe."
        ),
        _UpdateTemplateArgs,
        _make_update_handler(
            _UpdateTemplateArgs, _template_storage, "WorkspaceTemplate"
        ),
    )
    registry[name] = entry
    name, entry = _tool(
        "delete_workspace_template",
        (
            "Delete a WorkspaceTemplate. Existing Workspaces that "
            "referenced it keep their snapshot ``template_id`` but the "
            "row no longer resolves."
        ),
        _IdArgs,
        _make_delete_handler(_template_storage, "WorkspaceTemplate"),
    )
    registry[name] = entry

    # ------------------- Workspace CRUD (no update) -------------------
    name, entry = _tool(
        "list_workspaces",
        "List persisted Workspace rows with pagination.",
        _PaginationArgs,
        _make_list_handler(_workspace_storage),
    )
    registry[name] = entry
    name, entry = _tool(
        "get_workspace",
        "Fetch one Workspace row by id; ``type=not-found`` if missing.",
        _IdArgs,
        _make_get_handler(_workspace_storage, "Workspace"),
    )
    registry[name] = entry

    async def _create_workspace(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _CreateWorkspaceArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        template = await _template_storage().get(args.template_id)
        if template is None:
            return _err(
                f"WorkspaceTemplate {args.template_id!r} does not exist",
                error_type="not-found",
            )
        if args.id is not None:
            existing = await _workspace_storage().get(args.id)
            if existing is not None:
                return _err(
                    f"Workspace with id {args.id!r} already exists",
                    error_type="conflict",
                )
        try:
            live = await workspace_registry.materialise(
                template=template, overrides=args.overrides
            )
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="backend-error")
        row_id = args.id if args.id is not None else live.id
        row = WorkspaceRow(
            id=row_id,
            template_id=args.template_id,
            provider_id=template.provider_id,
            overrides=args.overrides,
            created_at=datetime.now(timezone.utc),
        )
        await _workspace_storage().create(row)
        return _ok(row)

    name, entry = _tool(
        "create_workspace",
        (
            "Materialise a new Workspace from a template. Looks up the "
            "template, asks the matching backend to create the live "
            "instance, then persists a Workspace row with the assigned "
            "id. Optional ``overrides`` layer per-instantiation env "
            "vars / files / init commands on top of the template."
        ),
        _CreateWorkspaceArgs,
        _create_workspace,
    )
    registry[name] = entry

    async def _delete_workspace(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            await workspace_registry.destroy(args.id)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="backend-error")
        return _ok({"deleted": True, "id": args.id})

    name, entry = _tool(
        "delete_workspace",
        (
            "Destroy a Workspace — backend resources AND the persisted "
            "row. ``type=not-found`` when the id is unknown."
        ),
        _IdArgs,
        _delete_workspace,
    )
    registry[name] = entry

    # ------------------- Sessions sub-resource ------------------------
    async def _list_sessions(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _WorkspaceListSessionsArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        sessions = await ws.list_sessions()
        sliced = sessions[args.offset : args.offset + args.limit]
        return _ok(
            {
                "items": [s.model_dump(mode="json") for s in sliced],
                "offset": args.offset,
                "length": len(sliced),
                "total": len(sessions),
            }
        )

    name, entry = _tool(
        "list_workspace_sessions",
        (
            "List sessions on a workspace, paginated. ``items`` is a "
            "list of SessionInfo objects; ``total`` is the full count."
        ),
        _WorkspaceListSessionsArgs,
        _list_sessions,
    )
    registry[name] = entry

    async def _get_session(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _WorkspaceSessionArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        session = await ws.get_session(args.session_id)
        if session is None:
            return _err(
                f"Session {args.session_id!r} does not exist on "
                f"workspace {args.workspace_id!r}",
                error_type="not-found",
            )
        info = await session.info()
        status = await session.status()
        return _ok(
            {
                "info": info.model_dump(mode="json"),
                "status": status.value if hasattr(status, "value") else str(status),
            }
        )

    name, entry = _tool(
        "get_workspace_session",
        (
            "Get session state — returns ``{info, status}`` where "
            "``info`` is the SessionInfo and ``status`` is the current "
            "lifecycle state (running / waiting / paused / ended)."
        ),
        _WorkspaceSessionArgs,
        _get_session,
    )
    registry[name] = entry

    def _session_op(op_name: str) -> ToolHandler:
        async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
            try:
                args = _WorkspaceSessionArgs.model_validate(arguments)
            except ValidationError as exc:
                return _err_from_validation(exc)
            try:
                ws = await workspace_registry.get_workspace(args.workspace_id)
            except NotFoundError as exc:
                return _err_from_matrix(exc, error_type="not-found")
            session = await ws.get_session(args.session_id)
            if session is None:
                return _err(
                    f"Session {args.session_id!r} does not exist on "
                    f"workspace {args.workspace_id!r}",
                    error_type="not-found",
                )
            method = getattr(session, op_name)
            try:
                await method()
            except MatrixError as exc:
                return _err_from_matrix(exc, error_type="conflict")
            return _ok({"ok": True, "session_id": args.session_id})

        return _handler

    name, entry = _tool(
        "pause_workspace_session",
        (
            "Request that a session pause at the next safe point. "
            "``type=conflict`` when the session is in an incompatible "
            "lifecycle state (already ended, etc.)."
        ),
        _WorkspaceSessionArgs,
        _session_op("request_pause"),
    )
    registry[name] = entry
    name, entry = _tool(
        "resume_workspace_session",
        (
            "Request that a paused session resume. ``type=conflict`` "
            "when the session is not currently paused."
        ),
        _WorkspaceSessionArgs,
        _session_op("request_resume"),
    )
    registry[name] = entry

    async def _steer_session(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _SteerArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        session = await ws.get_session(args.session_id)
        if session is None:
            return _err(
                f"Session {args.session_id!r} does not exist on "
                f"workspace {args.workspace_id!r}",
                error_type="not-found",
            )
        try:
            instruction = await session.append_instruction(args.instruction)
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="conflict")
        return _ok(instruction.model_dump(mode="json"))

    name, entry = _tool(
        "steer_workspace_session",
        (
            "Append a steering user instruction to a running session. "
            "The agent will see it on its next turn. Returns the "
            "appended Instruction object on success."
        ),
        _SteerArgs,
        _steer_session,
    )
    registry[name] = entry

    # ------------------- Files sub-resource ---------------------------
    async def _list_files(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _ListFilesArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            entries = await ws.list_files(args.path, recursive=args.recursive)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except (BadRequestError, MatrixError) as exc:
            return _err_from_matrix(exc, error_type="bad-request")
        sliced = entries[args.offset : args.offset + args.limit]
        return _ok(
            {
                "items": [e.model_dump(mode="json") for e in sliced],
                "offset": args.offset,
                "length": len(sliced),
                "total": len(entries),
                "path": args.path,
            }
        )

    name, entry = _tool(
        "list_workspace_files",
        (
            "List files in a workspace at ``path`` (default: root) "
            "with pagination. ``recursive=true`` walks the whole tree. "
            "Each item is a FileEntry (path, kind, size_bytes, "
            "modified_at)."
        ),
        _ListFilesArgs,
        _list_files,
    )
    registry[name] = entry

    async def _file_info(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _WorkspacePathArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            info = await ws.file_info(args.path)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except (BadRequestError, MatrixError) as exc:
            return _err_from_matrix(exc, error_type="bad-request")
        return _ok(info)

    name, entry = _tool(
        "get_workspace_file_info",
        (
            "Fetch the FileEntry for a single path (file / dir / "
            "symlink). ``type=not-found`` when missing; "
            "``type=bad-request`` on path-escape attempts."
        ),
        _WorkspacePathArgs,
        _file_info,
    )
    registry[name] = entry

    async def _read_file(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _ReadFileArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if args.encoding not in ("text", "base64"):
            return _err(
                f"unknown encoding {args.encoding!r}; use 'text' or 'base64'",
                error_type="bad-request",
            )
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            raw = await ws.read_file(args.path)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except (BadRequestError, MatrixError) as exc:
            return _err_from_matrix(exc, error_type="bad-request")
        if args.encoding == "text":
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return _err(
                    f"file at {args.path!r} is not valid UTF-8; request "
                    "encoding=base64 instead",
                    error_type="bad-request",
                )
        else:
            content = base64.b64encode(raw).decode("ascii")
        return _ok(
            {
                "path": args.path,
                "encoding": args.encoding,
                "content": content,
                "size_bytes": len(raw),
            }
        )

    name, entry = _tool(
        "read_workspace_file",
        (
            "Read a workspace file. ``encoding=text`` returns UTF-8 "
            "decoded content; ``encoding=base64`` returns the raw "
            "bytes as base64 (use this for binaries). Returns "
            "``{path, encoding, content, size_bytes}``."
        ),
        _ReadFileArgs,
        _read_file,
    )
    registry[name] = entry

    async def _delete_file(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _WorkspacePathArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            await ws.delete_file(args.path)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except (BadRequestError, MatrixError) as exc:
            return _err_from_matrix(exc, error_type="bad-request")
        return _ok({"deleted": True, "path": args.path})

    name, entry = _tool(
        "delete_workspace_file",
        (
            "Delete a file or empty directory. Refuses to delete the "
            "workspace root or paths inside ``.state`` / ``.tmp`` with "
            "``type=bad-request``."
        ),
        _WorkspacePathArgs,
        _delete_file,
    )
    registry[name] = entry

    async def _write_file(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _WriteFileArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if args.encoding not in ("text", "base64"):
            return _err(
                f"unknown encoding {args.encoding!r}; use 'text' or 'base64'",
                error_type="bad-request",
            )
        if args.encoding == "text":
            raw = args.content.encode("utf-8")
        else:
            try:
                raw = base64.b64decode(args.content, validate=True)
            except Exception as exc:  # noqa: BLE001
                return _err(
                    f"invalid base64 content: {exc}", error_type="bad-request"
                )
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            await ws.write_file(args.path, raw)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except (BadRequestError, MatrixError) as exc:
            return _err_from_matrix(exc, error_type="bad-request")
        return _ok({"path": args.path, "size_bytes": len(raw)})

    name, entry = _tool(
        "write_workspace_file",
        (
            "Replace (or create) a workspace file. ``encoding=text`` "
            "UTF-8 encodes the content; ``encoding=base64`` decodes the "
            "content first. Creates parent directories as needed. "
            "Refuses to overwrite a directory or write inside reserved "
            "``.state`` / ``.tmp`` trees."
        ),
        _WriteFileArgs,
        _write_file,
    )
    registry[name] = entry

    # ------------------- Log sub-resource ----------------------------
    async def _get_log(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _LogArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            commits = await ws.log(limit=args.limit)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="backend-error")
        return _ok({"commits": [c.model_dump(mode="json") for c in commits]})

    # watch_files — yielding tool (M4). Suspends the agent's turn until
    # one of the watched paths changes on disk. The matching watcher
    # (matrix/bus/watcher.py) polls mtimes and publishes change bursts.
    name, entry = _tool(
        "watch_files",
        (
            "Watch one or more workspace-relative paths and pause "
            "this agent turn until something changes. YIELDING TOOL "
            "— execution is suspended; the worker is released to "
            "handle other sessions while the watcher polls. Files "
            "and directories are both accepted; directories report "
            "child-file changes. Optional ``timeout_seconds`` (falls "
            "back to the global yield cap). Optional ``batch_window_ms"
            "`` (default 250) coalesces bursts of changes into one "
            "wake. Returns ``{timed_out: false, changes: [...]}`` on "
            "change, ``{timed_out: true, changes: []}`` on timeout, "
            "or ``{cancelled: true, ...}`` if the operator skipped "
            "the yield. Each change carries ``{path, event_type "
            "(created|modified|deleted), mtime_after}``."
        ),
        _WatchFilesArgs,
        _watch_files_handler,
    )
    registry[name] = entry

    name, entry = _tool(
        "get_workspace_log",
        (
            "Fetch up to ``limit`` recent commits from the workspace's "
            "``.state`` git repo. Newest first. Each commit carries the "
            "parsed ``X-Primer-*`` trailers (workspace, session, agent, "
            "op, tool, call) for structured rendering."
        ),
        _LogArgs,
        _get_log,
    )
    registry[name] = entry

    logger.info(
        "workspaces toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )
    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["WORKSPACES_TOOLSET_ID", "build_workspaces_toolset"]


# Register yielding-tool resume hooks at import time. The worker's
# resume path looks up hooks by tool name from this central registry.
from primer.worker.yield_resume_registry import register_resume_hook  # noqa: E402

register_resume_hook("watch_files", watch_files_resume)
