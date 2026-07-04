"""``workspaces`` reserved internal toolset — dogfoods the workspace API.

Always available (built once at app startup, like ``system``). The
internal collections subsystem ingests its tools into the
``_internal_tools`` collection during bootstrap so agents can search
for them.

Tool catalog
------------

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
    create_workspace_session, cancel_workspace_session,
    list_workspace_sessions, get_workspace_session,
    pause_workspace_session, resume_workspace_session,
    steer_workspace_session

Files:
    list_workspace_files, get_workspace_file_info,
    read_workspace_file, delete_workspace_file, write_workspace_file

Log:
    get_workspace_log

(The ``watch_files`` and ``invoke_graph`` yielding tools moved to the
``workspace_ext`` reserved toolset; their handlers + resume hooks remain
defined here and are imported by that module.)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import (
    BadRequestError,
    ConflictError,
    PrimerError,
    NotFoundError,
)
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.model.storage import CursorPage, OffsetPage, OrderBy
from primer.model.workspace import (
    Workspace as WorkspaceRow,
    WorkspaceProvider,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.model.workspace_session import SessionBinding, WorkspaceSession
from primer.model.yield_ import ToolContext, Yielded
from primer.tap.cursor import TapCursor
from primer.tap.reader import read_batch
from primer.tap.selector import TapSelector
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok as _ok
from primer.toolset.internal import InternalToolsetProvider, ToolHandler


if TYPE_CHECKING:
    from primer.api.registries import WorkspaceRegistry
    from primer.int.storage_provider import StorageProvider
    from primer.tap.router import WorkspaceTapRouter


logger = logging.getLogger(__name__)


WORKSPACES_TOOLSET_ID = "workspaces"


# ===========================================================================
# JSON / error helpers
# ===========================================================================


def _err_from_validation(exc: ValidationError) -> ToolCallResult:
    return _err(
        "argument validation failed: " + json.dumps(exc.errors(), default=str),
        error_type="validation-error",
    )


def _err_from_primer(exc: PrimerError, *, error_type: str) -> ToolCallResult:
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


class _CreateSessionArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    binding: SessionBinding
    initial_instructions: str | None = None
    auto_start: bool = True
    graph_input: Any | None = None
    parent_session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class _InvokeGraphArgs(BaseModel):
    graph_id: str = Field(..., min_length=1, description="Graph to run.")
    input: str = Field(..., min_length=1, description="Input for the graph.")


class _WorkspaceTapArgs(BaseModel):
    """Arguments for the ``workspace_tap`` drain tool.

    ``selector`` is a raw :class:`~primer.tap.selector.TapSelector` JSON
    object (``{sessions?, events?}``); ``cursor`` is the opaque
    batch-level resume token returned as ``next_cursor`` by a prior call
    (NOT the per-event ``cursor`` placeholder).
    """

    workspace_id: str = Field(..., min_length=1)
    selector: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional TapSelector as a JSON object with optional "
            "``sessions`` / ``events`` predicates. Scopes which sessions "
            "and events the drain returns. Malformed → a tool error."
        ),
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Opaque resume token from a prior call's ``next_cursor``. "
            "Absent / empty / garbage decodes to a fresh drain from the "
            "start. This is the batch-level cursor — NOT the per-event "
            "``cursor`` field, which is only a reader placeholder."
        ),
    )
    limit: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="Maximum events to return in one drain (1..1000).",
    )
    wait_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=30.0,
        description=(
            "Bounded long-poll. When the immediate drain is empty and "
            "``wait_seconds > 0``, wait up to this many seconds for a new "
            "tick, then drain ONCE more (may still be empty). ``0`` (the "
            "default) is non-blocking."
        ),
    )


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
        except (ConflictError, PrimerError) as exc:
            return _err_from_primer(exc, error_type="storage-error")
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
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
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
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        if on_delete is not None:
            await on_delete(args.id)
        return _ok({"deleted": True, "id": args.id})

    return _handler


def _tool(
    name: str,
    purpose: str,
    when: str,
    args_cls: type[BaseModel],
    handler: ToolHandler,
    examples: list[ToolExample],
    *,
    yields: bool = False,
    requires_session: bool = False,
    required_role: str | None = None,
) -> tuple[str, tuple[Tool, ToolHandler]]:
    return name, (
        make_tool(
            id=name,
            toolset_id=WORKSPACES_TOOLSET_ID,
            purpose=purpose,
            when=when,
            args_schema=args_cls.model_json_schema(),
            examples=examples,
            yields=yields,
            requires_session=requires_session,
            required_role=required_role,
        ),
        handler,
    )


# ===========================================================================
# watch_files — third yielding tool (M4 of the yielding-tools feature).
# See docs/superpowers/specs/2026-05-22-yielding-tools-design.md §8.3.
#
# Pauses the agent's turn until one of the watched paths changes on
# disk. The matching background watcher (primer/bus/watcher.py) polls
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
# invoke_graph - yielding tool (dynamic invocation). Runs a target graph
# inside the current workspace session, namespaced under the session's
# state (a child WorkspaceGraphExecutor), and returns its output text.
# Declared yielding (``-> ToolCallResult | Yielded``) so the provider
# classifies it as a session-only tool and excludes it from MCP; the
# happy (non-parking) path returns a ToolCallResult. Parking/resume is a
# later task - this handler does not yet surface a Yielded.
# ===========================================================================


async def _invoke_graph_handler(
    arguments: dict[str, Any],
    *,
    ctx: ToolContext,
) -> ToolCallResult | Yielded:
    from primer.agent.invoke import (
        InvocationDepthExceeded,
        invocation_depth_guard,
    )
    from primer.graph.invoke_graph import run_invoke_graph

    try:
        args = _InvokeGraphArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)
    if ctx.session_id is None or ctx.workspace_id is None:
        return _err(
            "invoke_graph is only available in workspace sessions",
            error_type="bad-request",
        )
    services = getattr(ctx, "graph_services", None)
    if services is None:
        return _err(
            "invoke_graph services are not available in this context",
            error_type="bad-request",
        )
    try:
        with invocation_depth_guard():
            text = await run_invoke_graph(
                graph_id=args.graph_id,
                graph_input=args.input,
                services=services,
                tool_call_id=ctx.tool_call_id,
            )
    except InvocationDepthExceeded as exc:
        return _err(
            f"invocation depth exceeded: {exc}", error_type="bad-request"
        )
    except (NotFoundError, ValueError) as exc:
        return _err(str(exc), error_type="bad-request")
    return _ok({"output": text})


# ===========================================================================
# Build the toolset
# ===========================================================================


def build_workspaces_toolset(
    *,
    storage_provider: "StorageProvider",
    workspace_registry: "WorkspaceRegistry",
    scheduler: "Any | None" = None,
    claim_engine: "Any | None" = None,
    event_bus: "Any | None" = None,
    tap_router: "WorkspaceTapRouter | None" = None,
    toolset_id: str = WORKSPACES_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the immutable ``_workspaces`` toolset.

    ``tap_router`` is the process-local
    :class:`~primer.tap.router.WorkspaceTapRouter` (lives on
    ``app.state.workspace_tap_router``); it is consulted ONLY by
    ``workspace_tap`` to implement the bounded long-poll. When ``None``
    the drain still works — it just degrades ``wait_seconds`` to a
    non-blocking single drain.
    """
    registry: dict[str, tuple[Tool, ToolHandler]] = {}

    def _provider_storage():
        return storage_provider.get_storage(WorkspaceProvider)

    def _template_storage():
        return storage_provider.get_storage(WorkspaceTemplate)

    def _workspace_storage():
        return storage_provider.get_storage(WorkspaceRow)

    def _session_row_storage():
        from primer.model.workspace_session import WorkspaceSession

        return storage_provider.get_storage(WorkspaceSession)

    async def _reconcile_session_info(info):
        """Overlay the durable session row's terminal status onto a slot view.

        ``get_workspace_session`` / ``list_workspace_sessions`` read the
        workspace's on-disk slot (``session.json``), but a session ended by
        the worker/dispatch (clean completion, cancel, fail-closed) updates
        the scheduler-visible ``WorkspaceSession`` row FIRST -- and on a
        different process / workspace-cache instance the slot can lag. The
        durable row is the same one ``GET /v1/sessions/{id}`` serves, so
        these MCP session tools stay faithful thin wrappers by preferring
        the row's status/ended_reason when the row is terminal but the slot
        view is not. Returns the (possibly updated) SessionInfo; never
        raises -- a storage miss degrades to the slot view unchanged.
        """
        from primer.model.workspace_session import SessionStatus

        if info.status == SessionStatus.ENDED:
            return info
        try:
            row = await _session_row_storage().get(info.session_id)
        except Exception:  # noqa: BLE001 -- advisory reconciliation
            return info
        if row is None or row.status != SessionStatus.ENDED:
            return info
        return info.model_copy(update={
            "status": SessionStatus.ENDED,
            "ended_reason": row.ended_reason,
            "ended_at": row.ended_at,
        })

    async def _inv_provider(eid: str) -> None:
        await workspace_registry.invalidate(eid)

    # ------------------- Provider CRUD (no update) ---------------------
    name, entry = _tool(
        "list_workspace_providers",
        (
            "List configured WorkspaceProviders with pagination. Returns "
            "``items``, ``length``, ``total`` (offset mode), "
            "``next_cursor`` (cursor mode)."
        ),
        (
            "Use when you need to enumerate the configured workspace "
            "backends; not for fetching one by id (use "
            "``get_workspace_provider``)."
        ),
        _PaginationArgs,
        _make_list_handler(_provider_storage),
        examples=[
            ToolExample(args={}, returns="page of WorkspaceProvider rows"),
            ToolExample(args={"limit": 50, "order_by": ["id:asc"]}),
        ],
        required_role="admin",
    )
    registry[name] = entry
    name, entry = _tool(
        "get_workspace_provider",
        (
            "Fetch one WorkspaceProvider by id. Returns the provider "
            "row with its discriminated config."
        ),
        (
            "Use when you have a provider id and want its full config; "
            "not for listing all of them (use "
            "``list_workspace_providers``). ``type=not-found`` when "
            "missing."
        ),
        _IdArgs,
        _make_get_handler(_provider_storage, "WorkspaceProvider"),
        examples=[
            ToolExample(args={"id": "local-1"}, returns="the WorkspaceProvider row"),
        ],
        required_role="admin",
    )
    registry[name] = entry
    name, entry = _tool(
        "create_workspace_provider",
        (
            "Create a new WorkspaceProvider. Body shape is the full "
            "WorkspaceProvider schema with ``provider`` discriminator "
            "and matching ``config``."
        ),
        (
            "Use when registering a new workspace backend. The Update "
            "operation is intentionally absent; to change a provider's "
            "config, delete and recreate."
        ),
        _CreateProviderArgs,
        _make_create_handler(
            _CreateProviderArgs, _provider_storage, "WorkspaceProvider"
        ),
        examples=[
            ToolExample(
                args={
                    "entity": {
                        "id": "local-1",
                        "provider": "local",
                        "config": {"kind": "local"},
                    }
                },
                returns="201 plus the stored provider row",
            ),
        ],
        required_role="admin",
    )
    registry[name] = entry
    name, entry = _tool(
        "delete_workspace_provider",
        (
            "Delete a WorkspaceProvider by id. Cascades to drop the "
            "cached backend instance from the WorkspaceRegistry."
        ),
        (
            "Use when retiring a configured backend; not for tearing "
            "down a materialised workspace (use ``delete_workspace``)."
        ),
        _IdArgs,
        _make_delete_handler(
            _provider_storage, "WorkspaceProvider", on_delete=_inv_provider
        ),
        examples=[
            ToolExample(args={"id": "local-1"}, returns="{deleted: true, id: ...}"),
        ],
        required_role="admin",
    )
    registry[name] = entry

    # ------------------- Template CRUD (full) -------------------------
    name, entry = _tool(
        "list_workspace_templates",
        "List WorkspaceTemplates with pagination.",
        (
            "Use when enumerating available materialisation recipes; "
            "not for fetching one by id (use "
            "``get_workspace_template``)."
        ),
        _PaginationArgs,
        _make_list_handler(_template_storage),
        examples=[
            ToolExample(args={}, returns="page of WorkspaceTemplate rows"),
            ToolExample(args={"limit": 10}),
        ],
        required_role="user",
    )
    registry[name] = entry
    name, entry = _tool(
        "get_workspace_template",
        "Fetch one WorkspaceTemplate by id.",
        (
            "Use when you have a template id and want its recipe; not "
            "for listing all of them (use "
            "``list_workspace_templates``). ``type=not-found`` if "
            "missing."
        ),
        _IdArgs,
        _make_get_handler(_template_storage, "WorkspaceTemplate"),
        examples=[
            ToolExample(args={"id": "py-base"}, returns="the WorkspaceTemplate row"),
        ],
        required_role="user",
    )
    registry[name] = entry
    name, entry = _tool(
        "create_workspace_template",
        (
            "Create a new WorkspaceTemplate. Body must reference an "
            "existing ``provider_id`` and include the materialisation "
            "recipe (backend, files, env, init_commands, resources)."
        ),
        (
            "Use when defining a new recipe to materialise workspaces "
            "from; not for materialising one (use ``create_workspace``)."
        ),
        _CreateTemplateArgs,
        _make_create_handler(
            _CreateTemplateArgs, _template_storage, "WorkspaceTemplate"
        ),
        examples=[
            ToolExample(
                args={
                    "entity": {
                        "id": "py-base",
                        "description": "Python base image",
                        "provider_id": "local-1",
                        "backend": {"kind": "local"},
                    }
                },
                returns="201 plus the stored template row",
            ),
        ],
        required_role="user",
    )
    registry[name] = entry
    name, entry = _tool(
        "update_workspace_template",
        (
            "Replace an existing WorkspaceTemplate. The body's ``id`` "
            "must equal the path ``id``."
        ),
        (
            "Use when editing a recipe in place. Existing materialised "
            "Workspaces are NOT re-materialised; only future creates "
            "see the new recipe."
        ),
        _UpdateTemplateArgs,
        _make_update_handler(
            _UpdateTemplateArgs, _template_storage, "WorkspaceTemplate"
        ),
        examples=[
            ToolExample(
                args={
                    "id": "py-base",
                    "entity": {
                        "id": "py-base",
                        "description": "Python base image (v2)",
                        "provider_id": "local-1",
                        "backend": {"kind": "local"},
                    },
                },
                returns="the updated template row",
            ),
        ],
        required_role="user",
    )
    registry[name] = entry
    name, entry = _tool(
        "delete_workspace_template",
        (
            "Delete a WorkspaceTemplate. Existing Workspaces that "
            "referenced it keep their snapshot ``template_id`` but the "
            "row no longer resolves."
        ),
        (
            "Use when retiring a recipe; not for destroying a "
            "materialised workspace (use ``delete_workspace``)."
        ),
        _IdArgs,
        _make_delete_handler(_template_storage, "WorkspaceTemplate"),
        examples=[
            ToolExample(args={"id": "py-base"}, returns="{deleted: true, id: ...}"),
        ],
        required_role="user",
    )
    registry[name] = entry

    # ------------------- Workspace CRUD (no update) -------------------
    name, entry = _tool(
        "list_workspaces",
        "List persisted Workspace rows with pagination.",
        (
            "Use when enumerating materialised workspaces; not for "
            "fetching one by id (use ``get_workspace``)."
        ),
        _PaginationArgs,
        _make_list_handler(_workspace_storage),
        examples=[
            ToolExample(args={}, returns="page of Workspace rows"),
            ToolExample(args={"limit": 20}),
        ],
        required_role="user",
    )
    registry[name] = entry
    name, entry = _tool(
        "get_workspace",
        "Fetch one Workspace row by id.",
        (
            "Use when you have a workspace id and want its persisted "
            "record; not for listing all of them (use "
            "``list_workspaces``). ``type=not-found`` if missing."
        ),
        _IdArgs,
        _make_get_handler(_workspace_storage, "Workspace"),
        examples=[
            ToolExample(args={"id": "ws-1"}, returns="the Workspace row"),
        ],
        required_role="user",
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
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="backend-error")
        row_id = args.id if args.id is not None else live.id
        row = WorkspaceRow(
            id=row_id,
            template_id=args.template_id,
            provider_id=template.provider_id,
            overrides=args.overrides,
            created_at=datetime.now(timezone.utc),
            runtime_meta=live.runtime_meta,
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
        (
            "Use when you need a live sandbox from an existing template; "
            "not for defining the recipe (use "
            "``create_workspace_template``)."
        ),
        _CreateWorkspaceArgs,
        _create_workspace,
        examples=[
            ToolExample(
                args={"template_id": "py-base"},
                returns="the stored Workspace row",
            ),
            ToolExample(
                args={
                    "id": "ws-1",
                    "template_id": "py-base",
                    "overrides": {"env": {"FOO": "bar"}},
                },
                note="overrides layer on top of the template",
            ),
        ],
        required_role="user",
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
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="backend-error")
        return _ok({"deleted": True, "id": args.id})

    name, entry = _tool(
        "delete_workspace",
        (
            "Destroy a Workspace, freeing both backend resources AND "
            "the persisted row."
        ),
        (
            "Use when tearing down a live workspace; not for retiring "
            "its template (use ``delete_workspace_template``). "
            "``type=not-found`` when the id is unknown."
        ),
        _IdArgs,
        _delete_workspace,
        examples=[
            ToolExample(args={"id": "ws-1"}, returns="{deleted: true, id: ...}"),
        ],
        required_role="user",
    )
    registry[name] = entry

    # ------------------- Sessions sub-resource ------------------------
    async def _create_session(arguments: dict[str, Any]) -> ToolCallResult:
        if scheduler is None or claim_engine is None:
            return _err(
                "session tools unavailable: scheduler/claim_engine not wired",
                error_type="unavailable",
            )
        try:
            args = _CreateSessionArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        from primer.workspace.session_factory import (
            SessionFactoryDeps,
            start_workspace_session,
        )

        deps = SessionFactoryDeps(
            storage_provider=storage_provider,
            claim_engine=claim_engine,
            scheduler=scheduler,
            workspace_registry=workspace_registry,
        )
        try:
            session = await start_workspace_session(
                workspace_id=args.workspace_id,
                binding=args.binding,
                initial_instructions=args.initial_instructions,
                graph_input=args.graph_input,
                auto_start=args.auto_start,
                metadata=args.metadata,
                parent_session_id=args.parent_session_id,
                deps=deps,
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except PrimerValidationError as exc:
            return _err_from_primer(exc, error_type="validation-error")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="backend-error")
        return _ok(session)

    name, entry = _tool(
        "create_workspace_session",
        "Start a session that runs an agent or graph inside a workspace.",
        (
            "Use when you need to execute an agent or graph headlessly; "
            "poll it with ``get_workspace_session`` and read outputs with "
            "``read_workspace_file``; stop it with "
            "``cancel_workspace_session``."
        ),
        _CreateSessionArgs,
        _create_session,
        examples=[
            ToolExample(
                args={
                    "workspace_id": "ws-1",
                    "binding": {"kind": "agent", "agent_id": "code-reviewer"},
                    "initial_instructions": "Summarise README.md",
                    "auto_start": True,
                },
                returns="the created session, e.g. {id, status:\"running\"}",
            ),
            ToolExample(
                args={
                    "workspace_id": "ws-1",
                    "binding": {"kind": "graph", "graph_id": "incident-pipeline"},
                    "graph_input": {"ticket": "INC-1"},
                },
                returns="a graph session bound to incident-pipeline",
            ),
        ],
        required_role="user",
    )
    registry[name] = entry

    async def _cancel_workspace_session(
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        if scheduler is None or claim_engine is None:
            return _err(
                "session tools unavailable: scheduler/claim_engine not wired",
                error_type="unavailable",
            )
        try:
            args = _WorkspaceSessionArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        from primer.workspace.session_factory import (
            SessionCancelDeps,
            cancel_session,
        )

        deps = SessionCancelDeps(
            storage_provider=storage_provider,
            scheduler=scheduler,
            claim_engine=claim_engine,
            event_bus=event_bus,
            workspace_registry=workspace_registry,
        )
        try:
            session = await cancel_session(
                workspace_id=args.workspace_id,
                session_id=args.session_id,
                deps=deps,
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except ConflictError as exc:
            return _err_from_primer(exc, error_type="conflict")
        return _ok(session)

    name, entry = _tool(
        "cancel_workspace_session",
        "Hard-cancel a session; it transitions to ended with reason cancelled.",
        (
            "Use when you need to stop a run; a created or paused session "
            "ends immediately, a running one is preempted at the next safe "
            "point. Not for a temporary halt (use "
            "``pause_workspace_session``)."
        ),
        _WorkspaceSessionArgs,
        _cancel_workspace_session,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "session_id": "ses-1"},
                returns=(
                    "the session, now "
                    "{status:\"ended\", ended_reason:\"cancelled\"}"
                ),
            ),
        ],
        required_role="user",
    )
    registry[name] = entry

    async def _list_sessions(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _WorkspaceListSessionsArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        sessions = await ws.list_sessions()
        sliced = sessions[args.offset : args.offset + args.limit]
        reconciled = [await _reconcile_session_info(s) for s in sliced]
        return _ok(
            {
                "items": [s.model_dump(mode="json") for s in reconciled],
                "offset": args.offset,
                "length": len(reconciled),
                "total": len(sessions),
            }
        )

    name, entry = _tool(
        "list_workspace_sessions",
        (
            "List sessions on a workspace, paginated. ``items`` is a "
            "list of SessionInfo objects; ``total`` is the full count."
        ),
        (
            "Use when enumerating the agent sessions on a workspace; "
            "not for one session's state (use "
            "``get_workspace_session``)."
        ),
        _WorkspaceListSessionsArgs,
        _list_sessions,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1"},
                returns="page of SessionInfo objects",
            ),
            ToolExample(args={"workspace_id": "ws-1", "limit": 10, "offset": 0}),
        ],
        required_role="user",
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
            return _err_from_primer(exc, error_type="not-found")
        session = await ws.get_session(args.session_id)
        if session is None:
            return _err(
                f"Session {args.session_id!r} does not exist on "
                f"workspace {args.workspace_id!r}",
                error_type="not-found",
            )
        info = await _reconcile_session_info(await session.info())
        status = info.status
        return _ok(
            {
                "info": info.model_dump(mode="json"),
                "status": status.value if hasattr(status, "value") else str(status),
            }
        )

    name, entry = _tool(
        "get_workspace_session",
        (
            "Get session state, returning ``{info, status}`` where "
            "``info`` is the SessionInfo and ``status`` is the current "
            "lifecycle state (running / waiting / paused / ended)."
        ),
        (
            "Use when inspecting one session on a workspace; not for "
            "listing all of them (use ``list_workspace_sessions``)."
        ),
        _WorkspaceSessionArgs,
        _get_session,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "session_id": "sess-1"},
                returns="{info, status}",
            ),
        ],
        required_role="user",
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
                return _err_from_primer(exc, error_type="not-found")
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
            except PrimerError as exc:
                return _err_from_primer(exc, error_type="conflict")
            return _ok({"ok": True, "session_id": args.session_id})

        return _handler

    name, entry = _tool(
        "pause_workspace_session",
        "Request that a session pause at the next safe point.",
        (
            "Use when you want to halt a running session; not to "
            "resume it (use ``resume_workspace_session``). "
            "``type=conflict`` when the session is in an incompatible "
            "lifecycle state (already ended, etc.)."
        ),
        _WorkspaceSessionArgs,
        _session_op("request_pause"),
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "session_id": "sess-1"},
                returns="{ok: true, session_id: ...}",
            ),
        ],
        required_role="user",
    )
    registry[name] = entry
    name, entry = _tool(
        "resume_workspace_session",
        "Request that a paused session resume.",
        (
            "Use when restarting a paused session; not to pause it "
            "(use ``pause_workspace_session``). ``type=conflict`` when "
            "the session is not currently paused."
        ),
        _WorkspaceSessionArgs,
        _session_op("request_resume"),
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "session_id": "sess-1"},
                returns="{ok: true, session_id: ...}",
            ),
        ],
        required_role="user",
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
            return _err_from_primer(exc, error_type="not-found")
        session = await ws.get_session(args.session_id)
        if session is None:
            return _err(
                f"Session {args.session_id!r} does not exist on "
                f"workspace {args.workspace_id!r}",
                error_type="not-found",
            )
        try:
            instruction = await session.append_instruction(args.instruction)
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="conflict")
        return _ok(instruction.model_dump(mode="json"))

    name, entry = _tool(
        "steer_workspace_session",
        (
            "Append a steering user instruction to a running session. "
            "The agent will see it on its next turn. Returns the "
            "appended Instruction object on success."
        ),
        (
            "Use when you want to nudge a live agent mid-run; not to "
            "pause or resume it (use ``pause_workspace_session`` / "
            "``resume_workspace_session``)."
        ),
        _SteerArgs,
        _steer_session,
        examples=[
            ToolExample(
                args={
                    "workspace_id": "ws-1",
                    "session_id": "sess-1",
                    "instruction": "Focus on the failing test first.",
                },
                returns="the appended Instruction object",
            ),
        ],
        required_role="user",
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
            return _err_from_primer(exc, error_type="not-found")
        except (BadRequestError, PrimerError) as exc:
            return _err_from_primer(exc, error_type="bad-request")
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
        (
            "Use when browsing a workspace's filesystem; not for one "
            "entry's metadata (use ``get_workspace_file_info``) or its "
            "bytes (use ``read_workspace_file``)."
        ),
        _ListFilesArgs,
        _list_files,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1"},
                returns="page of FileEntry objects at the root",
            ),
            ToolExample(
                args={"workspace_id": "ws-1", "path": "src", "recursive": True},
            ),
        ],
        required_role="user",
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
            return _err_from_primer(exc, error_type="not-found")
        except (BadRequestError, PrimerError) as exc:
            return _err_from_primer(exc, error_type="bad-request")
        return _ok(info)

    name, entry = _tool(
        "get_workspace_file_info",
        "Fetch the FileEntry for a single path (file / dir / symlink).",
        (
            "Use when you need one path's metadata (kind, size, mtime); "
            "not for listing a directory (use ``list_workspace_files``) "
            "or reading bytes (use ``read_workspace_file``). "
            "``type=not-found`` when missing; ``type=bad-request`` on "
            "path-escape attempts."
        ),
        _WorkspacePathArgs,
        _file_info,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "path": "src/main.py"},
                returns="the FileEntry",
            ),
        ],
        required_role="user",
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
            return _err_from_primer(exc, error_type="not-found")
        except (BadRequestError, PrimerError) as exc:
            return _err_from_primer(exc, error_type="bad-request")
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
        (
            "Use when you need a file's bytes; not for its metadata "
            "(use ``get_workspace_file_info``) or to write it (use "
            "``write_workspace_file``)."
        ),
        _ReadFileArgs,
        _read_file,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "path": "README.md"},
                returns="{path, encoding, content, size_bytes}",
            ),
            ToolExample(
                args={"workspace_id": "ws-1", "path": "logo.png", "encoding": "base64"},
                note="use base64 for binary files",
            ),
        ],
        required_role="user",
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
            return _err_from_primer(exc, error_type="not-found")
        except (BadRequestError, PrimerError) as exc:
            return _err_from_primer(exc, error_type="bad-request")
        return _ok({"deleted": True, "path": args.path})

    name, entry = _tool(
        "delete_workspace_file",
        "Delete a file or empty directory in a workspace.",
        (
            "Use when removing one path from a workspace; not for "
            "destroying the whole workspace (use ``delete_workspace``). "
            "Refuses to delete the workspace root or paths inside "
            "``.state`` / ``.tmp`` with ``type=bad-request``."
        ),
        _WorkspacePathArgs,
        _delete_file,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "path": "tmp/scratch.txt"},
                returns="{deleted: true, path: ...}",
            ),
        ],
        required_role="user",
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
            return _err_from_primer(exc, error_type="not-found")
        except (BadRequestError, PrimerError) as exc:
            return _err_from_primer(exc, error_type="bad-request")
        return _ok({"path": args.path, "size_bytes": len(raw)})

    name, entry = _tool(
        "write_workspace_file",
        (
            "Replace (or create) a workspace file. ``encoding=text`` "
            "UTF-8 encodes the content; ``encoding=base64`` decodes the "
            "content first. Creates parent directories as needed."
        ),
        (
            "Use when creating or overwriting file content; not for "
            "reading it (use ``read_workspace_file``). Refuses to "
            "overwrite a directory or write inside reserved ``.state`` "
            "/ ``.tmp`` trees."
        ),
        _WriteFileArgs,
        _write_file,
        examples=[
            ToolExample(
                args={
                    "workspace_id": "ws-1",
                    "path": "notes.txt",
                    "content": "hello world",
                },
                returns="{path, size_bytes}",
            ),
            ToolExample(
                args={
                    "workspace_id": "ws-1",
                    "path": "logo.png",
                    "content": "iVBORw0KGgo=",
                    "encoding": "base64",
                },
                note="base64 content is decoded to raw bytes",
            ),
        ],
        required_role="user",
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
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="backend-error")
        return _ok({"commits": [c.model_dump(mode="json") for c in commits]})

    # NOTE: ``watch_files`` and ``invoke_graph`` moved to the
    # ``workspace_ext`` reserved toolset (context optimization: keep
    # workspace-only yielding tools out of chat context). Their handlers,
    # arg models, and the watch_files resume hook still live in this
    # module and are imported by ``primer.toolset.workspace_ext``; only
    # the tool descriptors were re-homed under the new toolset id.

    name, entry = _tool(
        "get_workspace_log",
        (
            "Fetch up to ``limit`` recent commits from the workspace's "
            "``.state`` git repo, newest first. Each commit carries the "
            "parsed ``X-Primer-*`` trailers (workspace, session, agent, "
            "op, tool, call) for structured rendering."
        ),
        (
            "Use when you need the workspace's state-repo history; not "
            "for a session's lifecycle state (use "
            "``get_workspace_session``)."
        ),
        _LogArgs,
        _get_log,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1"},
                returns="{commits: [CommitInfo, ...]}",
            ),
            ToolExample(args={"workspace_id": "ws-1", "limit": 10}),
        ],
        required_role="user",
    )
    registry[name] = entry

    # ------------------- Tap drain (read-only) -----------------------
    async def _workspace_tap(arguments: dict[str, Any]) -> ToolCallResult:
        """Drain tap events for a workspace; advance the batch cursor.

        Request/response cursor-drain over the shared
        :func:`primer.tap.reader.read_batch` engine — the MCP analogue
        of the SSE tap (MCP cannot stream). Resolves the session store +
        live workspace IO exactly as the SSE endpoint does, decodes the
        selector / cursor, drains once, and (when the drain is empty and
        ``wait_seconds > 0``) does a single bounded long-poll on the tap
        router before draining once more.
        """
        try:
            args = _WorkspaceTapArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)

        # Decode the selector (empty when absent; malformed → tool error).
        try:
            selector = (
                TapSelector.model_validate(args.selector)
                if args.selector is not None
                else TapSelector()
            )
        except ValidationError as exc:
            return _err(
                "invalid selector: " + json.dumps(exc.errors(), default=str),
                error_type="bad-request",
            )

        # Tolerant decode: absent / empty / garbage → fresh drain.
        cursor = TapCursor.decode(args.cursor)

        # Resolve the live workspace IO the same way the SSE endpoint does.
        try:
            workspace_io = await workspace_registry.get_workspace(
                args.workspace_id
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")

        sessions_storage = storage_provider.get_storage(WorkspaceSession)

        async def _drain() -> list:
            # Rebind the enclosing ``cursor`` to the one ``read_batch``
            # RETURNS (not the input we passed in). ``read_batch`` happens to
            # advance the cursor in place today, but binding the return value
            # keeps ``next_cursor`` correct if a future ``read_batch`` ever
            # hands back a fresh cursor instead of mutating the argument.
            nonlocal cursor
            events, cursor = await read_batch(
                sessions_storage,
                workspace_io,
                workspace_id=args.workspace_id,
                selector=selector,
                cursor=cursor,
                limit=args.limit,
            )
            return events

        events = await _drain()

        # Bounded long-poll: only when the immediate drain is empty AND
        # the caller asked to wait AND a router is wired. This never parks
        # the agent turn — it is a single timed wait then one more drain.
        if not events and args.wait_seconds > 0 and tap_router is not None:
            sub = tap_router.subscribe(args.workspace_id)
            try:
                try:
                    await asyncio.wait_for(
                        sub.__anext__(), timeout=args.wait_seconds
                    )
                except (TimeoutError, StopAsyncIteration):
                    # Timed out (or the subscription ended) — fall through
                    # to one more drain, which may still be empty.
                    pass
            finally:
                await sub.aclose()
            events = await _drain()

        return _ok(
            {
                "events": [e.model_dump(mode="json", by_alias=True) for e in events],
                "next_cursor": cursor.encode(),
            }
        )

    name, entry = _tool(
        "workspace_tap",
        (
            "Drain tap events (user input, assistant tokens, tool calls / "
            "results, lifecycle) for every in-scope session in a "
            "workspace since the last cursor, then advance it. Returns "
            "``{events: [TapEvent...], next_cursor}``. Resume the next "
            "drain by passing ``next_cursor`` back as ``cursor`` — the "
            "per-event ``cursor`` field is a placeholder, NOT the resume "
            "token. ``wait_seconds`` enables a bounded long-poll when the "
            "drain would otherwise be empty."
        ),
        (
            "Use when polling a workspace's activity from outside an agent "
            "loop (the request/response analogue of the SSE tap stream). "
            "Page with ``next_cursor``; an empty ``events`` with the same "
            "``next_cursor`` means nothing new. Not for one session's "
            "lifecycle state (use ``get_workspace_session``)."
        ),
        _WorkspaceTapArgs,
        _workspace_tap,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1"},
                returns="{events: [...], next_cursor: \"<token>\"}",
            ),
            ToolExample(
                args={"workspace_id": "ws-1", "cursor": "<prev next_cursor>"},
                note="resume from a prior drain — only newer events return",
            ),
            ToolExample(
                args={
                    "workspace_id": "ws-1",
                    "selector": {
                        "events": {
                            "left": {"kind": "field", "name": "class"},
                            "op": "=",
                            "right": {"kind": "value", "value": "tool_call"},
                        }
                    },
                    "limit": 50,
                    "wait_seconds": 5,
                },
                note="filter to tool_call events; long-poll up to 5s",
            ),
        ],
        required_role="user",
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
