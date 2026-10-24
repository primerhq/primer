"""``workspaces`` reserved internal toolset — dogfoods the workspace API.

Always available (built once at app startup, like ``system``). The
internal collections subsystem ingests its tools into the
``_internal_tools`` collection during bootstrap so agents can search
for them.

Tool catalog
------------

Provider (CRUD minus update):
    list_workspace_providers, get_workspace_provider,
    create_workspace_provider, delete_workspace_provider,
    list_workspace_provider_choices (user tier: id, kind and label only)

Template (full CRUD):
    list_workspace_templates, get_workspace_template,
    create_workspace_template, update_workspace_template,
    delete_workspace_template

Workspace (CRUD minus update):
    list_workspaces, get_workspace, create_workspace, delete_workspace

Sessions:
    create_workspace_session, cancel_workspace_session,
    interrupt_workspace_session,
    list_workspace_sessions, get_workspace_session,
    pause_workspace_session, resume_workspace_session,
    steer_workspace_session, restart_workspace_session,
    find_sessions,
    list_my_sessions, message_session

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
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from primer.agent.inner_dispatch import run_principal
from primer.authz import _role_allows
from primer.common.validation_errors import without_input
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.common import preserve_masked_secrets, refuse_served_masks
from primer.model.except_ import (
    BadRequestError,
    ConfigError,
    ConflictError,
    PrimerError,
    NotFoundError,
)
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.model.storage import (
    CursorPage,
    FieldRef,
    OffsetPage,
    Op,
    OrderBy,
    Predicate,
    Value,
)
from primer.storage.secret_fields import refuse_secret_fields
from primer.model.workspace import (
    DEFAULT_GPU_RESOURCE_NAME,
    DOCKER_RUNTIME_IMAGE,
    MAX_GPU_COUNT,
    MAX_WORKSPACE_CPU_CORES,
    MAX_WORKSPACE_DISK_BYTES,
    MAX_WORKSPACE_MEMORY_BYTES,
    WORKSPACE_ID_PATTERN,
    Workspace as WorkspaceRow,
    WorkspaceProvider,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.model.workspace_refusal import ReservedWorkspaceError
from primer.model.workspace_session import (
    SessionBinding,
    SessionState,
    SessionStatus,
    WorkspaceSession,
    session_state_predicate,
)
from primer.model.yield_ import ToolContext, Yielded
from primer.session.peer_messages import (
    PEER_MESSAGE_MAX_CHARS,
    REFUSE_RESTART_NEEDS_INPUT,
    PeerMessageRefused,
    PeerSendResult,
    in_reach,
    list_workspace_session_summaries,
    reach_of,
    send_peer_message,
)
from primer.tap.cursor import TapCursor
from primer.tap.reader import read_batch
from primer.tap.selector import TapSelector, TapSelectorError, TapSelectorTimeout, check_selector
from primer.session.slot_view import overlay_row_on_slot_info, overlay_rows_on_infos
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok as _ok
from primer.toolset._system_guards import (
    CrudGuards,
    ToolReference,
    refuse_create,
    refuse_delete,
    refuse_delete_id,
    refuse_delete_if_referenced,
    refuse_update,
)
from primer.toolset.internal import InternalToolsetProvider, ToolHandler
from primer.workspace.env_names import ENV_NAME_PATTERN, MAX_ENV_NAME_LENGTH, RUNTIME_TOKEN_ENV_NAMES
from primer.workspace.provider_choices import list_provider_choices
from primer.workspace.reserved import reserved_tree, reserved_tree_for, reserved_trees
from primer.workspace.template_privilege import (
    admin_only_override_fields,
    admin_only_settings_held,
    admin_only_template_changes,
    held_refusal_message,
    refusal_message,
)
from primer.workspace.template_secrets import restore_template_secrets


if TYPE_CHECKING:
    from primer.api.registries import WorkspaceRegistry
    from primer.int.storage_provider import StorageProvider
    from primer.tap.router import WorkspaceTapRouter


logger = logging.getLogger(__name__)


WORKSPACES_TOOLSET_ID = "workspaces"

#: The workspace a calling session is confined to when its workspace cannot be told: an id no workspace has (an id is never empty), so it reaches
#: nothing (fail closed).
_NO_WORKSPACE = ""


# ===========================================================================
# JSON / error helpers
# ===========================================================================


def _err_from_validation(exc: ValidationError) -> ToolCallResult:
    return _err(
        "argument validation failed: " + json.dumps(without_input(exc.errors()), default=str),
        error_type="validation-error",
    )


def _err_from_primer(exc: PrimerError, *, error_type: str) -> ToolCallResult:
    return _err(getattr(exc, "message", str(exc)), error_type=error_type)


def _caller_is_admin(ctx: ToolContext | None) -> bool:
    """Whether the caller of this tool call is an admin: the ONE rule for every admin check in this toolset (the reserved ``.state`` /
    ``.tmp`` reads of A-22 and the admin-only template fields).

    A call dispatched with a ToolContext (an agent run, a task) is judged by that run's ``initiated_by`` and by nothing else: a context
    that carries no initiator is an unknown caller, NOT the request's, so a task spawned from an MCP request does not inherit that
    request's actor. A call dispatched with NO ToolContext (the MCP endpoint) is judged by the request's actor
    (``mcp.server.current_actor``). With neither the caller is unknown and the answer is no (fail closed). The floor is ``_role_allows``,
    the tool role floor's own predicate: an admin passes, the internal ``system`` actor always passes, and since A-20 a ``trigger``-typed
    actor is ranked by its role like any other (it used to be waved through).
    """
    if ctx is not None:
        actor: Any = ctx.initiated_by
    else:
        from primer.mcp.server import current_actor
        actor = current_actor.get()
    return _role_allows(actor, "admin")


def _caller_principal(ctx: ToolContext | None) -> Any:
    """Who is calling, for a rank rule: the run's initiator when the call has a ToolContext, else (the MCP endpoint) the request's actor; ``None``
    when neither is known, which :func:`primer.authz.principal_rank` ranks as an ordinary user. The two sources of :func:`_caller_is_admin`."""
    if ctx is not None:
        return ctx.initiated_by
    from primer.mcp.server import current_actor

    return current_actor.get()


def _refuse_outranked(ctx: ToolContext | None, row: WorkspaceSession) -> ToolCallResult | None:
    """``forbidden`` when the caller ranks below the creator of ``row`` (lead ruling 2 of the G7 fixes, audit G7-06; review of #853), else ``None``.

    The caller must out-rank or equal the session's creator, the rule of the REST routes, of a trigger's fire and of a message from another session
    (:func:`primer.authz.may_drive`): for the steer and restart tools called with no calling session, and for the cancel and resume tools from a session
    or not. The message is fixed: it names neither the creator nor the rank. A call with no ToolContext is the MCP endpoint, a live request whose actor
    carries the account's current role, so it may always act on a session its own account or token started (``own_session``, N1); a run may not
    (a trigger-capped run carries an admin's identity at a lower rank).
    """
    from primer.authz import OUTRANKED_MESSAGE, may_drive, rank_label
    from primer.session.peer_messages import REFUSE_RANK

    if may_drive(_caller_principal(ctx), row.initiated_by, own_session=ctx is None):
        return None
    if ctx is not None and ctx.session_id:
        # From a session: the words a message, a steer and a restart from a session are refused in (one refusal text, review of #853 round 2).
        return _err(
            REFUSE_RANK.format(session_id=row.id, target_rank=rank_label(row.initiated_by), sender_rank=rank_label(run_principal(ctx))),
            error_type="forbidden",
        )
    return _err(OUTRANKED_MESSAGE, error_type="forbidden")


def _caller_actor(ctx: ToolContext | None) -> str:
    """The actor an event raised by this tool call is attributed to: the run's initiator, or, on the MCP endpoint (no ToolContext), the
    request's actor; ``system`` when neither is known. The same two sources, with the same split, as :func:`_caller_is_admin`."""
    from primer.events.recorder import actor_of

    if ctx is not None:
        return actor_of(ctx.initiated_by)
    from primer.mcp.server import current_actor

    return actor_of(current_actor.get())


def _unknown_workspace(workspace_id: str) -> NotFoundError:
    """What a workspace that does not exist answers, in the registry's words (``WorkspaceRegistry.get_workspace_row``): an out-of-reach workspace
    answers exactly this, so its id is never confirmed (lead ruling 3 of the G7 fixes)."""
    return NotFoundError(f"Workspace {workspace_id!r} does not exist")


def _session_not_on_workspace(args: "_WorkspaceSessionArgs") -> str:
    return f"Session {args.session_id!r} does not exist on workspace {args.workspace_id!r}"


def _outside_a_workspace_session(tool_name: str) -> ToolCallResult:
    """The refusal of a session-scoped tool called with no calling workspace session: no tool context at all (what the MCP endpoint dispatches with),
    or a context with no session or no workspace (a chat). ``list_my_sessions`` and ``message_session`` take WHO is calling from the context and
    nowhere else, so without one there is nothing to list or to send from."""
    return _err(f"{tool_name} works only inside a workspace session", error_type="unsupported-surface")


def _refuse_reserved_read(
    ws: Any, path: str, ctx: ToolContext | None,
) -> ToolCallResult | None:
    """A ``forbidden`` error when a non-admin names a path in the runtime's
    ``.state`` / ``.tmp`` trees (A-22), else ``None``."""
    if _caller_is_admin(ctx):
        return None
    tree = reserved_tree_for(ws, path)
    if tree is None:
        return None
    return _err(
        f"{path!r} is inside the workspace runtime's reserved {tree!r} tree; "
        "only an admin may read it",
        error_type="forbidden",
    )


def _refuse_non_admin(fields: list[str], ctx: ToolContext | None) -> ToolCallResult | None:
    """The REST rule on admin-only template fields (security review 2026-10-08): refuse a non-admin that writes them."""
    if fields and not _caller_is_admin(ctx):
        return _err(refusal_message(fields), error_type="forbidden")
    return None


def _template_delete_refusal(
    _entity: None, existing: WorkspaceTemplate, ctx: ToolContext | None,
) -> ToolCallResult | None:
    """Deleting a template that holds admin-only settings is admin-only too, as on REST."""
    held = admin_only_settings_held(existing)
    if held and not _caller_is_admin(ctx):
        return _err(held_refusal_message(held, "delete"), error_type="forbidden")
    return None


def _template_refusal(
    entity: WorkspaceTemplate, existing: WorkspaceTemplate | None, ctx: ToolContext | None,
) -> ToolCallResult | None:
    """Admin-only template fields first, then the Kubernetes overlay allowlist, which binds admins too.

    A stored template that HOLDS an admin-only value is not user-editable at all, as on REST.
    """
    held = admin_only_settings_held(existing)
    if held and not _caller_is_admin(ctx):
        return _err(held_refusal_message(held), error_type="forbidden")
    refusal = _refuse_non_admin(admin_only_template_changes(entity, existing), ctx)
    if refusal is not None:
        return refusal
    from primer.workspace.k8s.backend import _validate_template_overrides
    try:
        _validate_template_overrides(entity)
    except ConfigError as exc:
        return _err(str(exc), error_type="validation-error")
    return None


def _restore_template_secrets(entity: WorkspaceTemplate, existing: WorkspaceTemplate | None, ctx: ToolContext | None) -> None:
    """The REST rule on the masks of a served template body (01a11d32): an admin's password comes back for the same origin and user, a non-admin's only for the whole unchanged URL."""
    restore_template_secrets(entity, existing, admin=_caller_is_admin(ctx))


#: Work carried on after its caller was cancelled. A strong reference: asyncio keeps tasks weakly, so without this a
#: teardown could be garbage-collected half done.
_CARRIED_ON: set[asyncio.Task] = set()


async def _carry_on(work: Awaitable[Any], *, what: str) -> Any:
    """Await ``work`` as its own task that runs to its end even when the caller is cancelled.

    A cancel of the caller is raised here at once, so the cancellation semantics are unchanged, but ``work`` is not
    cancelled with it: a multi-step teardown (the instance, then its sessions, then the row) must not stop between two
    steps and leave a row pointing at a dead instance. If ``work`` then fails, nobody is awaiting it, so the failure is
    logged here rather than lost.
    """
    task = asyncio.ensure_future(work)
    _CARRIED_ON.add(task)
    task.add_done_callback(_CARRIED_ON.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            task.add_done_callback(lambda t: _log_carried_on_outcome(t, what=what))
        raise


def _log_carried_on_outcome(task: asyncio.Task, *, what: str) -> None:
    if task.cancelled() or task.exception() is None:
        return
    logger.error("%s: failed after its caller was cancelled", what, exc_info=task.exception())


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


class _NoArgs(BaseModel):
    """A tool that takes no arguments."""


class _CreateProviderArgs(BaseModel):
    entity: WorkspaceProvider = Field(..., description="WorkspaceProvider body.")


class _CreateTemplateArgs(BaseModel):
    entity: WorkspaceTemplate = Field(..., description="WorkspaceTemplate body.")


class _UpdateTemplateArgs(BaseModel):
    id: str = Field(..., min_length=1)
    entity: WorkspaceTemplate = Field(...)


class _CreateWorkspaceArgs(BaseModel):
    # Same rule as REST (WorkspaceCreateBody.id): the id names a directory
    # under the workspace root, so a traversal id must not reach the backend.
    id: str | None = Field(default=None, pattern=WORKSPACE_ID_PATTERN)
    template_id: str = Field(..., min_length=1)
    overrides: WorkspaceTemplateOverrides | None = Field(default=None)


class _CreateSessionArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    binding: SessionBinding
    initial_instructions: str | None = None
    auto_start: bool = True
    autonomous: bool | None = None
    graph_input: Any | None = None
    parent_session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class _WorkspaceSessionArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)


class _SessionMessagesArgs(_WorkspaceSessionArgs):
    after_seq: int | None = Field(
        default=None, ge=0,
        description="Return only records with seq above this (poll with the last seq you saw).",
    )
    limit: int = Field(default=200, ge=1, le=1000)
    offset: int = Field(
        default=0, ge=0,
        description="Skip this many records (from the end when ``tail`` is true), as the REST route.",
    )
    tail: bool = Field(
        default=False,
        description="Return the most recent ``limit`` records instead of the oldest.",
    )


class _SteerArgs(_WorkspaceSessionArgs):
    instruction: str = Field(..., min_length=1)


class _RestartArgs(_WorkspaceSessionArgs):
    input: str | None = Field(
        default=None,
        description="Optional new initial input for the re-opened session.",
    )


class _WorkspaceListSessionsArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class _FindSessionsArgs(BaseModel):
    workspace_id: str | None = Field(None, min_length=1)
    agent_id: str | None = Field(None, min_length=1)
    state: SessionState | None = None
    initiated_by: str | None = Field(
        None,
        min_length=1,
        description="The initiating principal's id (PrincipalRef.id)",
    )
    limit: int = Field(20, ge=1, le=200)


class _ListMySessionsArgs(BaseModel):
    state: SessionState | None = Field(
        default=None,
        description="Only the sessions in this state (waiting, running, parked or ended); omit it for all of them.",
    )
    limit: int = Field(default=50, ge=1, le=200, description="How many sessions to return, newest first.")


class _MessageSessionArgs(BaseModel):
    session_id: str = Field(..., min_length=1, description="The session to message: an id from list_my_sessions.")
    text: str = Field(
        ...,
        min_length=1,
        max_length=PEER_MESSAGE_MAX_CHARS,
        description="The message, 1 to 32,000 characters. Write larger outputs to a file and send its path.",
    )
    mode: Literal["auto", "if_idle"] = Field(
        default="auto",
        description=(
            "auto delivers by the session's state (an idle session starts its next turn with it, a busy one reads it at its next turn boundary). "
            "if_idle sends only when the session would start a turn now, and is refused (conflict) when it is busy, waiting or paused."
        ),
    )


class _WorkspacePathArgs(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


# 01a0645c: bounds a recursive=True walk's own cost, same magnitude and
# reasoning as the HTTP files route's _MAX_RECURSIVE_WALK_ENTRIES
# (primer/api/routers/workspaces.py, 01a0644b) - a private module
# constant each, not shared, since these are unrelated call sites that
# happen to want the same safety margin.
_MAX_RECURSIVE_WALK_ENTRIES = 10_000


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
            "and events the drain returns. Malformed → a tool error. An "
            "``events`` LIKE / ILIKE pattern is at most 1000 characters, "
            "and one drain may spend 1 s matching them."
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
# What the workspace and template tools say about providers
# (platform revamp 2026-10, plan 08 task 8, design 14.4)
# ===========================================================================
#
# An agent that writes a template or creates a workspace reads the description and nothing else, so each description says what the server
# does with a field per provider. The numbers and names are built from the constants the server enforces, so a limit that moves moves the
# description with it (tests/toolset/test_workspace_tool_provider_notes.py derives its expectations from the same constants).

_TIB = 2**40
_RUNTIME_TOKEN_NAMES = " and ".join(f"``{name}``" for name in sorted(RUNTIME_TOKEN_ENV_NAMES))

# Lead ruling 17 of plan 08 (final): an environment variable name is what Kubernetes accepts, at most 256 characters; the runtime token's two names are
# the platform's own.
_ENV_NAME_RULE = (
    f"An environment variable name follows Kubernetes' own rule: ``{ENV_NAME_PATTERN}``, at most {MAX_ENV_NAME_LENGTH} characters, and never "
    f"``.``, ``..`` or a name that starts with ``..``; {_RUNTIME_TOKEN_NAMES} are reserved for the runtime's own token. "
    "A bad name is ``type=validation-error`` and the message names it (never its value)."
)

_CREATE_WORKSPACE_PURPOSE = (
    "Materialise a new Workspace from a template. Looks up the template, asks the matching backend to create the live "
    "instance, then persists a Workspace row with the assigned id and a full create-time template snapshot used for re-attachment. "
    "Optional ``overrides`` layer per-instantiation env "
    "vars / files / init commands on top of the template. "
    "``overrides.resources`` sizes THIS workspace instead of the template: ``cpu_cores`` and ``memory_bytes`` on Kubernetes "
    "and Docker, ``disk_bytes`` on Kubernetes only (it is the PVC size). A Local workspace takes none of the three and "
    "Docker refuses ``disk_bytes`` (it has no disk quota): the answer is ``type=validation-error``, the message names "
    "``overrides.resources.<field>``, and nothing is created. "
    f"A size is a positive number: at most {MAX_WORKSPACE_CPU_CORES} CPU cores, {MAX_WORKSPACE_MEMORY_BYTES // _TIB} TiB of memory "
    f"and {MAX_WORKSPACE_DISK_BYTES // _TIB} TiB of disk. "
    "On Kubernetes ``overrides.env`` reaches the pod itself, not only the start-up commands. " + _ENV_NAME_RULE
)

_CREATE_WORKSPACE_WHEN = (
    "Use when you need a live sandbox from an existing template; not for defining the recipe (use "
    "``create_workspace_template``). "
    f"A Docker workspace runs the template's ``backend.image`` (``{DOCKER_RUNTIME_IMAGE}`` when the template names none); if the "
    "runtime in the container does not answer within 30 seconds the create fails (``type=backend-error``, REST 503) with a "
    "message that names the image, and nothing is left behind. "
    f"A custom image must contain the primer workspace runtime: build it FROM ``{DOCKER_RUNTIME_IMAGE}``."
)

# Appended to the `when` of create_workspace_template and update_workspace_template: which backend field a provider reads, who may set it, and the limits.
_TEMPLATE_FIELDS_BY_PROVIDER = (
    "Which fields a template carries depends on its provider: ``backend.kind`` must match the provider's kind "
    "(``list_workspace_provider_choices`` lists the ids and kinds you may use). "
    "Local (``backend`` ``{\"kind\": \"local\"}``) runs on the server itself in the provider's root folder: it has no image and no size. "
    f"Docker (``backend.kind=container``): ``backend.image`` is the image that runs (``{DOCKER_RUNTIME_IMAGE}`` when omitted), "
    "``backend.cpu_cores`` and ``backend.memory_bytes`` size it, and there is no disk setting. "
    f"A custom image must contain the primer workspace runtime (build it FROM ``{DOCKER_RUNTIME_IMAGE}``); if the runtime does not "
    "answer within 30 seconds ``create_workspace`` fails (REST 503, ``type=backend-error``) with a message that names the image. "
    "Kubernetes (``backend.kind=kubernetes``): ``backend.image`` is required, and ``backend.entrypoint`` is optional: "
    'with none the pod starts ``["python", "-m", "primer_runtime.server"]``; an explicit entrypoint wins and ``backend.args`` '
    "is appended. "
    "``resources.cpu_cores`` and ``resources.memory_bytes`` are the pod's requests and limits (the old ``backend.cpu_request``, "
    "``cpu_limit``, ``memory_request`` and ``memory_limit`` are ignored), ``backend.pvc_size`` is the disk, a Kubernetes quantity "
    f"with no space (for example ``10Gi``), more than zero and at most {MAX_WORKSPACE_DISK_BYTES // _TIB} TiB, "
    "and the placement fields are ``node_selector`` (label key to value), ``tolerations`` (``key``, ``operator``, ``value``, "
    "``effect``), ``gpu`` (``{count, resource_name}``), ``service_account_name`` and ``image_pull_policy`` (``Always``, "
    "``IfNotPresent`` or ``Never``). "
    f"The ``gpu`` resource name is ``{DEFAULT_GPU_RESOURCE_NAME}`` unless you name another, and ``count`` is 1 to {MAX_GPU_COUNT}. "
    "The admin-only fields (a non-admin who sets or changes one gets ``type=forbidden``, and cannot update or delete a template that "
    "holds one) are ``tolerations``, ``service_account_name``, "
    f"a ``gpu.resource_name`` other than ``{DEFAULT_GPU_RESOURCE_NAME}``, container ``backend.extra_mounts``, Kubernetes "
    "``extra_volumes``, ``extra_volume_mounts`` and ``pod_overrides``, and a file whose source ``kind`` is ``secret``. "
    "``node_selector``, a ``gpu`` count with the default resource name and ``image_pull_policy`` are open to every user. "
    "A GPU resource name under ``kubernetes.io/`` or starting with ``requests.`` is refused for everyone "
    "(``type=validation-error``). "
    "A template CPU or memory of zero or less, and a ``pvc_size`` that is not such a quantity, are refused when the template is "
    "written (``type=validation-error``). "
    f"A template whose CPU or memory is above {MAX_WORKSPACE_CPU_CORES} cores or {MAX_WORKSPACE_MEMORY_BYTES // _TIB} TiB is refused "
    "(``type=validation-error``) when a workspace is created from it. A template ``env`` name follows the same rule as "
    "``overrides.env``. " + _ENV_NAME_RULE
)

def _k8s_template_example(*, description: str, image_pull_policy: str) -> dict[str, Any]:
    """The Kubernetes example of both template tools: a working one (it sets the entrypoint that starts the runtime) that a role=user caller
    may write (nothing in it is admin-only)."""
    return {
        "id": "k8s-gpu",
        "description": description,
        "provider_id": "k3s",
        "backend": {
            "kind": "kubernetes",
            "image": DOCKER_RUNTIME_IMAGE,
            "entrypoint": ["python", "-m", "primer_runtime.server"],
            "node_selector": {"accelerator": "nvidia"},
            "gpu": {"count": 1},
            "image_pull_policy": image_pull_policy,
            "pvc_size": "20Gi",
        },
        "resources": {"cpu_cores": 4, "memory_bytes": 17179869184},
    }


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
#: ``(entity, existing or None, ctx) -> refusal or None``: a write rule that depends on who the caller is.
_PrivilegeCheck = Callable[[Any, Any, "ToolContext | None"], ToolCallResult | None] | None
#: ``(entity, existing, ctx) -> None``: puts the stored secrets back into a full-replace body (raises ``ValidationError`` for a mask that cannot be restored); the default is ``preserve_masked_secrets``.
_RestoreSecrets = Callable[[Any, Any, "ToolContext | None"], None] | None


#: ``(page or row) -> payload``: what a list or get tool serves instead of the stored page or row (a response-only field added to each row).
_Served = Callable[[Any], Awaitable[Any]] | None
#: ``ctx -> predicate or None``: the rows a caller's listing is confined to, ``None`` for every row (the reach of a calling session).
_ListScope = Callable[["ToolContext | None"], Awaitable[Predicate | None]] | None
#: ``(ctx, id) -> bool``: whether the caller may see the row ``id``; a row it may not is answered exactly as a row that does not exist.
_GetScope = Callable[["ToolContext | None", str], Awaitable[bool]] | None


def _make_list_handler(
    storage_factory: Callable[[], Any], model_cls: type, *, served: _Served = None, scope: _ListScope = None,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _PaginationArgs.model_validate(arguments)
            page = _parse_page(args)
            order_by = _parse_order_by(args.order_by)
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                return _err_from_validation(exc)
            return _err(str(exc), error_type="bad-request")
        try:
            refuse_secret_fields(model_cls, order_by=order_by)
        except PrimerValidationError as exc:
            return _err(exc.message, error_type="validation-error")
        try:
            confined = await scope(ctx) if scope is not None else None
            storage = storage_factory()
            if confined is None:
                listed = await storage.list(page, order_by=order_by)
            else:
                listed = await storage.find(confined, page, order_by=order_by)
            return _ok(await served(listed) if served is not None else listed)
        except BadRequestError as exc:
            # A bad cursor (forged, or not this request's) is a client error,
            # answered like the secret-field refusal above (ticket 01a1212a).
            return _err(exc.message, error_type="validation-error")
        except PrimerError as exc:
            # A genuine storage fault is a tool error, as in the system tools,
            # not an exception out of call().
            return _err_from_primer(exc, error_type="storage-error")

    return _handler


def _make_get_handler(
    storage_factory: Callable[[], Any], cls_name: str, *, served: _Served = None, scope: _GetScope = None,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        visible = scope is None or await scope(ctx, args.id)
        row = await storage_factory().get(args.id) if visible else None
        if row is None:
            return _err(
                f"{cls_name} {args.id!r} does not exist", error_type="not-found"
            )
        return _ok(await served(row) if served is not None else row)

    return _handler


def _make_create_handler(
    args_cls: type[BaseModel],
    storage_factory: Callable[[], Any],
    cls_name: str,
    on_create: _OnMutate = None,
    guards: CrudGuards | None = None,
    privilege_check: _PrivilegeCheck = None,
) -> ToolHandler:
    guards = guards or CrudGuards(kind=cls_name)

    async def _handler(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = args_cls.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        entity = args.entity  # type: ignore[attr-defined]
        refusal = refuse_create(guards, entity)
        if refusal is None and privilege_check is not None:
            refusal = privilege_check(entity, None, ctx)
        if refusal is not None:
            return refusal
        # A create has nothing stored to restore a mask from: the copy-a-row move (get_*, a new id, create_*) would store the served masks as the values.
        try:
            refuse_served_masks(entity)
        except PrimerValidationError as exc:          # the REST twin is a 422
            return _err(exc.message, error_type="validation-error")
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
    guards: CrudGuards | None = None,
    privilege_check: _PrivilegeCheck = None,
    restore: _RestoreSecrets = None,
) -> ToolHandler:
    guards = guards or CrudGuards(kind=cls_name)

    async def _handler(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
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
        existing = await storage.get(args.id)  # type: ignore[attr-defined]
        if existing is None:
            return _err(
                f"{cls_name} {args.id!r} does not exist", error_type="not-found"  # type: ignore[attr-defined]
            )
        refusal = refuse_update(guards, entity, existing)
        if refusal is None and privilege_check is not None:
            refusal = privilege_check(entity, existing, ctx)
        if refusal is not None:
            return refusal
        # get_* serves a masked secret (a url file source's password, an env value) and this is a full replace, so a caller that reads a row and writes it back would store the mask: the REST
        # routers run the same helper as an on_pre_update hook. A secret the caller really changed replaces the stored one; a mask that cannot be restored for this caller is refused (01a11d32).
        try:
            if restore is not None:
                restore(entity, existing, ctx)
            else:
                preserve_masked_secrets(entity, existing)
        except PrimerValidationError as exc:
            return _err(exc.message, error_type="validation-error")
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
    *,
    storage_provider: "StorageProvider",
    on_delete: _OnMutate = None,
    guards: CrudGuards | None = None,
    privilege_check: _PrivilegeCheck = None,
) -> ToolHandler:
    guards = guards or CrudGuards(kind=cls_name)

    async def _handler(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        # A reserved id is protected whether or not a row exists, so this runs before the lookup (REST's on_pre_delete_id).
        refusal = refuse_delete_id(guards, args.id)
        if refusal is not None:
            return refusal
        storage = storage_factory()
        existing = await storage.get(args.id)
        if existing is None:
            return _err(
                f"{cls_name} {args.id!r} does not exist", error_type="not-found"
            )
        refusal = refuse_delete(guards, existing)
        if refusal is None and privilege_check is not None:
            refusal = privilege_check(None, existing, ctx)
        if refusal is None:
            refusal = await refuse_delete_if_referenced(guards, existing, storage_provider)
        if refusal is not None:
            return refusal
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
    requires_workspace: bool = False,
    required_role: str | None = None,
    interruptible: bool = True,
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
            requires_workspace=requires_workspace,
            required_role=required_role,
            interruptible=interruptible,
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
    ctx: "ResumeContext",
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
    from primer.graph.invoke_graph import ChildGraphFailed, run_invoke_graph

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
    except ChildGraphFailed as failed:
        # The child ended failed: an error result the model can act on, the same body a resumed child delivers.
        return ToolCallResult(output=failed.result_json(), is_error=True)
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

    def _session_rows_or_none():
        """The session row store, or ``None`` when the storage provider cannot hand it out: the overlay is advisory, so an
        unreachable store serves the slot as it is (and says so) and never turns into a tool error."""
        try:
            return _session_row_storage()
        except Exception:  # noqa: BLE001 -- advisory: the slot is still an answer
            logger.warning("workspace session tools: the session row store is unavailable; serving the slot as it is", exc_info=True)
            return None

    async def _overlay_row(info, workspace_id: str):
        rows = _session_rows_or_none()
        if rows is None:
            return info
        return await overlay_row_on_slot_info(info, rows, workspace_id=workspace_id)

    async def _overlay_rows(infos, workspace_id: str):
        rows = _session_rows_or_none()
        if rows is None:
            return list(infos)
        return await overlay_rows_on_infos(infos, rows, workspace_id=workspace_id)

    async def _inv_provider(eid: str) -> None:
        await workspace_registry.invalidate(eid)

    # What the REST workspace routers guard (task 01a111d1, D5 phase 1): the reserved bootstrap rows and a provider a template or a
    # workspace still references. A workspace carries a create-time template snapshot, so its provenance template is not a delete
    # dependency.
    # Imported here: the registry and the bootstrap defaults import toolsets, so module-level imports would be cycles.
    from primer.api.registries.provider_registry import RESERVED_WORKSPACE_PROVIDER_IDS
    from primer.bootstrap.defaults import RESERVED_WORKSPACE_TEMPLATES

    _reserved_templates = frozenset(RESERVED_WORKSPACE_TEMPLATES)
    _reserved_template_names = ", ".join(f"``{template_id}``" for template_id in sorted(_reserved_templates))
    provider_guards = CrudGuards(
        kind="workspace_provider",
        reserved_create_ids=RESERVED_WORKSPACE_PROVIDER_IDS,
        reserved_update_ids=RESERVED_WORKSPACE_PROVIDER_IDS,
        reserved_delete_ids=RESERVED_WORKSPACE_PROVIDER_IDS,
        references=(
            ToolReference("workspace_template", WorkspaceTemplate, "provider_id"),
            ToolReference("workspace", WorkspaceRow, "provider_id"),
        ),
    )

    template_guards = CrudGuards(
        kind="workspace_template",
        reserved_create_ids=_reserved_templates,
        reserved_update_ids=_reserved_templates,
        reserved_delete_ids=_reserved_templates,
    )

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
        _make_list_handler(_provider_storage, WorkspaceProvider),
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
            _CreateProviderArgs, _provider_storage, "WorkspaceProvider", guards=provider_guards,
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
            _provider_storage, "WorkspaceProvider",
            storage_provider=storage_provider, on_delete=_inv_provider, guards=provider_guards,
        ),
        examples=[
            ToolExample(args={"id": "local-1"}, returns="{deleted: true, id: ...}"),
        ],
        required_role="admin",
    )
    registry[name] = entry

    async def _list_workspace_provider_choices(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            _NoArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            return _ok(await list_provider_choices(_provider_storage()))
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")

    name, entry = _tool(
        "list_workspace_provider_choices",
        (
            "List every workspace provider by id, kind and label, sorted by id: ``items`` of ``{id, kind, label, default_image}``. "
            "``kind`` is ``local``, ``container`` or ``kubernetes``; ``label`` is Local, Docker, Podman, containerd or Kubernetes; "
            "``default_image`` is the image a container provider runs when a template names none (``primer/workspace-runtime:1.1``), "
            "null for the other kinds. Never returns a provider's configuration. Takes no arguments."
        ),
        (
            "Use when you are not an admin and need the id to put in a template's ``provider_id`` "
            "(``create_workspace_template``), or to see which kinds of workspace this deployment offers: "
            "``list_workspace_providers`` is admin-only because a provider row holds its connection. Not for reading a "
            "provider's configuration (an admin uses ``get_workspace_provider``)."
        ),
        _NoArgs,
        _list_workspace_provider_choices,
        examples=[
            ToolExample(
                args={},
                returns=(
                    '{"items": [{"id": "docker-1", "kind": "container", "label": "Docker", '
                    '"default_image": "primer/workspace-runtime:1.1"}]}'
                ),
            ),
        ],
        required_role="user",
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
        _make_list_handler(_template_storage, WorkspaceTemplate),
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
            "from; not for materialising one (use ``create_workspace``). "
            + _TEMPLATE_FIELDS_BY_PROVIDER
        ),
        _CreateTemplateArgs,
        _make_create_handler(
            _CreateTemplateArgs, _template_storage, "WorkspaceTemplate", guards=template_guards,
            privilege_check=_template_refusal,
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
            ToolExample(
                args={"entity": _k8s_template_example(description="GPU workspace on the gpu nodes", image_pull_policy="IfNotPresent")},
                returns="the stored template row",
                note="Kubernetes: node_selector, a gpu count and image_pull_policy are open to every user",
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
            "see the new recipe. A ``kind=url`` file source's password is "
            "returned masked (``https://user:**********@host/x``), and so is an "
            "``env`` value: send the body you read back and the stored secrets "
            "are kept (the files are matched by their path). An admin keeps a "
            "password for the same scheme, host, port and user; any other caller "
            "keeps it only when the whole URL is unchanged. A masked URL that "
            "cannot be restored answers ``type=validation-error`` (re-enter "
            "the password). "
            + _TEMPLATE_FIELDS_BY_PROVIDER
        ),
        _UpdateTemplateArgs,
        _make_update_handler(
            _UpdateTemplateArgs, _template_storage, "WorkspaceTemplate", guards=template_guards,
            privilege_check=_template_refusal, restore=_restore_template_secrets,
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
            ToolExample(
                args={
                    "id": "k8s-gpu",
                    "entity": _k8s_template_example(description="GPU workspace, always pull", image_pull_policy="Always"),
                },
                returns="the updated template row",
                note="Kubernetes: the body replaces the whole row, so send every field you keep",
            ),
        ],
        required_role="user",
    )
    registry[name] = entry
    name, entry = _tool(
        "delete_workspace_template",
        (
            "Delete a WorkspaceTemplate. Existing workspaces keep their create-time template snapshot and are unaffected."
        ),
        (
            "Use when retiring a recipe; not for destroying a "
            "materialised workspace (use ``delete_workspace``). "
            "A template that holds admin-only settings is refused to a non-admin (``type=forbidden``) before this "
            f"delete, and a built-in template ({_reserved_template_names}) is never deletable (``type=forbidden``)."
        ),
        _IdArgs,
        _make_delete_handler(
            _template_storage, "WorkspaceTemplate", storage_provider=storage_provider, guards=template_guards,
            privilege_check=_template_delete_refusal,
        ),
        examples=[
            ToolExample(args={"id": "py-base"}, returns="{deleted: true, id: ...}"),
        ],
        required_role="user",
    )
    registry[name] = entry

    # ------------------- Workspace CRUD (no update) -------------------
    async def _default_workspace_id() -> str | None:
        from primer.bootstrap.prime import resolve_default_workspace_id

        return await resolve_default_workspace_id(storage_provider)

    def _with_is_default(row: WorkspaceRow, default_id: str | None) -> dict[str, Any]:
        """A served workspace row: the stored row plus ``is_default``, the response-only flag the REST rows carry (G7-09, lead ruling 5): an
        agent finds the default workspace (prime) by it, never by its name, which is a label anyone may give any workspace."""
        return {**row.model_dump(mode="json"), "is_default": row.id == default_id}

    async def _workspace_page_served(page: Any) -> dict[str, Any]:
        default_id = await _default_workspace_id()
        return {**page.model_dump(mode="json"), "items": [_with_is_default(row, default_id) for row in page.items]}

    async def _workspace_row_served(row: WorkspaceRow) -> dict[str, Any]:
        return _with_is_default(row, await _default_workspace_id())

    async def _caller_reach(ctx: ToolContext | None) -> str | None:
        """The one workspace the caller may reach, or ``None`` for every workspace (lead ruling 3 of the G7 fixes, audit G7-05).

        A call from a workspace session reaches its own workspace, and every workspace when that is the default one (prime, the management
        plane): :func:`primer.session.peer_messages.reach_of`, the reach ``message_session`` and the steer and restart tools apply. A call with no
        calling session (a person over MCP, an API client) keeps its reach over every workspace.
        """
        if ctx is None or not ctx.session_id:
            return None
        workspace_id = ctx.workspace_id
        if not workspace_id:
            row = await _session_row_storage().get(ctx.session_id)
            workspace_id = row.workspace_id if row is not None else _NO_WORKSPACE
        return await reach_of(storage_provider, workspace_id)

    async def _out_of_reach(ctx: ToolContext | None, workspace_id: str) -> ToolCallResult | None:
        """The answer to a call on ``workspace_id`` from a session that may not reach it, else ``None`` (ticket 01a16ad1).

        Exactly the not-found of a workspace that does not exist, in the registry's words (:func:`_unknown_workspace`), so a workspace id seen in a
        file or a log is never confirmed. Every tool that names a workspace asks this at the point where it would look the workspace up, after its
        own argument checks, so an out-of-reach id and an unknown one answer alike whatever else the call says.
        """
        if await _workspace_in_reach(ctx, workspace_id):
            return None
        return _err_from_primer(_unknown_workspace(workspace_id), error_type="not-found")

    async def _workspace_in_reach(ctx: ToolContext | None, workspace_id: str) -> bool:
        """Whether the caller may reach ``workspace_id``; ``get_workspace`` answers a row it may not exactly as a missing one."""
        return in_reach(await _caller_reach(ctx), workspace_id)

    async def _workspaces_in_reach(ctx: ToolContext | None) -> Predicate | None:
        """``list_workspaces`` confined to the caller's reach: only its own workspace from a session outside prime, every one otherwise."""
        reach = await _caller_reach(ctx)
        return None if reach is None else Predicate(left=FieldRef(name="id"), op=Op.EQ, right=Value(value=reach))

    name, entry = _tool(
        "list_workspaces",
        (
            "List persisted Workspace rows with pagination. Each row carries "
            "``is_default``: true for the default workspace (prime)."
        ),
        (
            "Use when enumerating materialised workspaces, or to find the "
            "default workspace (the row with ``is_default: true``; never by "
            "its name, which any workspace may carry); not for fetching one "
            "by id (use ``get_workspace``). From a session outside the "
            "default workspace (prime) it lists only your own workspace."
        ),
        _PaginationArgs,
        _make_list_handler(_workspace_storage, WorkspaceRow, served=_workspace_page_served, scope=_workspaces_in_reach),
        examples=[
            ToolExample(args={}, returns="page of Workspace rows"),
            ToolExample(args={"limit": 20}),
        ],
        required_role="user",
    )
    registry[name] = entry
    name, entry = _tool(
        "get_workspace",
        (
            "Fetch one Workspace row by id, with ``is_default``: true for "
            "the default workspace (prime)."
        ),
        (
            "Use when you have a workspace id and want its persisted "
            "record; not for listing all of them (use "
            "``list_workspaces``). ``type=not-found`` if missing. " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
        ),
        _IdArgs,
        _make_get_handler(_workspace_storage, "Workspace", served=_workspace_row_served, scope=_workspace_in_reach),
        examples=[
            ToolExample(args={"id": "ws-1"}, returns="the Workspace row"),
        ],
        required_role="user",
    )
    registry[name] = entry

    async def _create_workspace(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _CreateWorkspaceArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        # A secret file source in the overrides reads an operator secret by name: admin only, as on REST.
        refusal = _refuse_non_admin(admin_only_override_fields(args.overrides), ctx)
        if refusal is not None:
            return refusal
        # The overrides are kept on the workspace row and served masked (overrides.env): the copy-a-workspace move would start the workspace with the mask as the variable's value (01a1212a, round 3 of #711).
        try:
            refuse_served_masks(args.overrides)
        except PrimerValidationError as exc:
            return _err(exc.message, error_type="validation-error")
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
                template=template,
                overrides=args.overrides,
                workspace_id=args.id,
            )
        except PrimerValidationError as exc:
            # A refusal of the request itself (a size the provider cannot honour, master Review Focus 5): the message
            # names overrides.resources.<field>, and nothing was built.
            return _err(exc.message, error_type="validation-error")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="backend-error")
        # materialise() created a live instance and the durable row is written LAST, so a failure or a cancel before
        # the row exists would orphan the instance: the probe loop is row-driven and nothing sweeps orphans. Same
        # rollback as the REST route (primer/api/routers/workspaces.py), and BaseException for the same reason: a
        # cancel is the likeliest way in (a hard Cancel of the turn cancels every call whatever its flag).
        try:
            row_id = args.id if args.id is not None else live.id
            row = WorkspaceRow(
                id=row_id,
                template_id=args.template_id,
                template_snapshot=template,
                provider_id=template.provider_id,
                overrides=args.overrides,
                created_at=datetime.now(timezone.utc),
                runtime_meta=live.runtime_meta,
            )
            await _workspace_storage().create(row)
        except BaseException:
            # registry.destroy() is row-driven and the row does not exist, so tear the instance down on the backend
            # directly. Best effort: a failed rollback never masks the original error or the cancel.
            try:
                backend = await workspace_registry.get_backend(template.provider_id)
                await backend.destroy(live.id)
            except Exception:
                logger.exception(
                    "create_workspace: failed to roll back orphaned live workspace %s", live.id,
                )
            raise
        return _ok(row)

    name, entry = _tool(
        "create_workspace",
        _CREATE_WORKSPACE_PURPOSE,
        _CREATE_WORKSPACE_WHEN,
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
            ToolExample(
                args={
                    "template_id": "k8s-python",
                    "overrides": {"resources": {"cpu_cores": 4, "memory_bytes": 8589934592, "disk_bytes": 21474836480}},
                },
                returns="the stored Workspace row",
                note="a Kubernetes workspace sized for this one workspace; on Docker leave out disk_bytes",
            ),
        ],
        required_role="user",
        # The live instance, then the row: two steps with no transaction. A Stop does not cancel the call; the handler
        # also rolls the instance back if it is cancelled anyway (the flag governs a Stop only, not a hard Cancel).
        interruptible=False,
    )
    registry[name] = entry

    async def _delete_workspace(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        # Out of reach before the default-workspace refusal: prime is out of reach of a session outside it, and is answered as unknown too.
        refusal = await _out_of_reach(ctx, args.id)
        if refusal is not None:
            return refusal
        try:
            # Carried to its end if this call is cancelled: destroy() is the instance, its sessions, then the row.
            await _carry_on(workspace_registry.destroy(args.id), what=f"delete_workspace {args.id}")
        except ReservedWorkspaceError as exc:
            # The default workspace (prime) is never deleted; destroy() refuses it before touching anything. Before the generic
            # PrimerError branch below, which would call a refusal a backend failure.
            return _err_from_primer(exc, error_type="forbidden")
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
            "``type=not-found`` when the id is unknown; "
            "``type=forbidden`` for the default workspace (prime), "
            "which cannot be deleted. From a session outside the default "
            "workspace (prime) only your own workspace is reachable: "
            "another one (prime included) answers type=not-found, as an "
            "unknown id does."
        ),
        _IdArgs,
        _delete_workspace,
        examples=[
            ToolExample(args={"id": "ws-1"}, returns="{deleted: true, id: ...}"),
        ],
        required_role="user",
        # Tear the instance down, reconcile its sessions, delete the row: three steps with no transaction. A Stop does
        # not cancel the call; the handler also carries the teardown to its end if it is cancelled anyway (a hard Cancel).
        interruptible=False,
    )
    registry[name] = entry

    # ------------------- Sessions sub-resource ------------------------
    async def _create_session(
        arguments: dict[str, Any], ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        if scheduler is None or claim_engine is None:
            return _err(
                "session tools unavailable: scheduler/claim_engine not wired",
                error_type="unavailable",
            )
        try:
            args = _CreateSessionArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if not in_reach(await _caller_reach(ctx), args.workspace_id):
            # A session outside the default workspace creates sessions only in its own (review of #853): a session it made in prime would reach
            # every workspace. Answered exactly as a workspace that does not exist.
            return _err_from_primer(_unknown_workspace(args.workspace_id), error_type="not-found")
        from primer.model.principal import PrincipalRef
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
                autonomous=args.autonomous,
                # No ctx (the MCP endpoint hands handlers none) means no
                # known caller: fail closed to an ordinary user, never the
                # system principal that clears every role floor (A-20).
                initiated_by=(
                    getattr(ctx, "initiated_by", None)
                    or PrincipalRef.unattributed()
                ),
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
            "``cancel_workspace_session``. From a session outside the "
            "default workspace (prime) only your own workspace is "
            "reachable: another one answers type=not-found, as an unknown "
            "workspace does."
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
        # Several writes with no common transaction (state repo commit, session row, enqueue, claim lease): a cancel
        # between them can leave a RUNNING session with no lease. A Stop waits for the call (same for the cancel,
        # steer and restart tools below).
        interruptible=False,
    )
    registry[name] = entry

    async def _cancel_workspace_session(
        arguments: dict[str, Any], ctx: ToolContext | None = None,
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
        # The reach of a calling session, then the rank of the caller (review of #853): out of reach answers exactly as an unknown session does
        # (cancel_session's own words), and a caller below the session's creator is refused before anything is written.
        if not in_reach(await _caller_reach(ctx), args.workspace_id):
            return _err(_session_not_on_workspace(args), error_type="not-found")
        target = await _session_row_storage().get(args.session_id)
        if target is not None and target.workspace_id == args.workspace_id:
            refusal = _refuse_outranked(ctx, target)
            if refusal is not None:
                return refusal
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
            "``pause_workspace_session``). " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does. type=forbidden when the session runs at a higher rank than you (or your run)."
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
        interruptible=False,
    )
    registry[name] = entry

    async def _interrupt_workspace_session(
        arguments: dict[str, Any], ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        # No scheduler / claim engine check (unlike the tools above): a Stop touches neither, only the session row and the bus.
        try:
            args = _WorkspaceSessionArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if not in_reach(await _caller_reach(ctx), args.workspace_id):
            # Out of the calling session's reach (review of #853): exactly as an unknown session (interrupt_session's own words).
            return _err(_session_not_on_workspace(args), error_type="not-found")
        from primer.workspace.session_factory import (
            SessionInterruptDeps,
            interrupt_session,
        )

        deps = SessionInterruptDeps(
            storage_provider=storage_provider, event_bus=event_bus,
        )
        try:
            session = await interrupt_session(
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
        "interrupt_workspace_session",
        (
            "Stop (interrupt) the turn a session is running without ending "
            "the session: it lands in waiting with its history intact, and "
            "the next message continues it."
        ),
        (
            "Use when a running turn should stop but the session should stay "
            "alive; not for ending a session (use "
            "``cancel_workspace_session``: a parent waiting for a stopped "
            "child to END waits on, because the child is still alive). The "
            "answer is the session row, and a success answer alone is NOT a "
            "Stop: ``interrupt_requested: true`` means a Stop was recorded, "
            "``interrupt_requested: false`` means nothing was running and "
            "nothing was recorded (the session was idle, paused or not "
            "started). ``type=conflict`` when the session has ended or is "
            "parked waiting on an approval, an answer or a timer (cancel it "
            "instead). A session may stop itself; an autonomous session that "
            "does waits until something wakes it, and a graph session stops "
            "between graph events, not at the next model call. " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
        ),
        _WorkspaceSessionArgs,
        _interrupt_workspace_session,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "session_id": "ses-1"},
                returns=(
                    "the session, with {interrupt_requested: true} when a "
                    "turn was running and the Stop is recorded"
                ),
            ),
        ],
        required_role="user",
        # interruptible stays True on purpose (see tests/toolset/test_interruptible_declarations.py): one guarded row write
        # (the flag) then a best-effort publish. The flag is durable and the worker polls it, so a cancel between the two
        # loses only the fast path, never the Stop.
    )
    registry[name] = entry

    async def _list_sessions(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _WorkspaceListSessionsArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if not in_reach(await _caller_reach(ctx), args.workspace_id):
            # Out of the calling session's reach: answered exactly as a workspace that does not exist (the registry's own words for one), so a
            # workspace id seen in a file or a log is never confirmed.
            return _err_from_primer(_unknown_workspace(args.workspace_id), error_type="not-found")
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        sessions = await ws.list_sessions()
        sliced = sessions[args.offset : args.offset + args.limit]
        # The lifecycle fields come from the durable row, the one truth (primer.session.slot_view), not from the slot.
        reconciled = await _overlay_rows(sliced, args.workspace_id)
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
            "list of SessionInfo objects (their lifecycle fields come from "
            "the durable session row); ``total`` is the full count."
        ),
        (
            "Use when enumerating the agent sessions on a workspace; "
            "not for one session's state (use "
            "``get_workspace_session``). From a session outside the default "
            "workspace (prime) only your own workspace is reachable: another "
            "one answers type=not-found, as an unknown id does."
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

    async def _find_sessions(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _FindSessionsArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        filters: list[Predicate] = []
        reach = await _caller_reach(ctx)
        if reach is not None:
            # A session outside the default workspace finds only its own workspace's sessions (lead ruling 3 of the G7 fixes). Another workspace
            # named in ``workspace_id`` then finds nothing, exactly as a workspace id that does not exist.
            filters.append(
                Predicate(left=FieldRef(name="workspace_id"), op=Op.EQ, right=Value(value=reach))
            )
        if args.workspace_id is not None:
            filters.append(
                Predicate(
                    left=FieldRef(name="workspace_id"),
                    op=Op.EQ,
                    right=Value(value=args.workspace_id),
                )
            )
        if args.agent_id is not None:
            filters.append(
                Predicate(
                    left=FieldRef(name="binding.agent_id"),
                    op=Op.EQ,
                    right=Value(value=args.agent_id),
                )
            )
        if args.state is not None:
            filters.append(session_state_predicate(args.state))
        if args.initiated_by is not None:
            filters.append(
                Predicate(
                    left=FieldRef(name="initiated_by.id"),
                    op=Op.EQ,
                    right=Value(value=args.initiated_by),
                )
            )
        predicate = None
        for part in filters:
            predicate = part if predicate is None else Predicate(
                left=predicate, op=Op.AND, right=part
            )
        page = await _session_row_storage().find(
            predicate,
            OffsetPage(offset=0, length=args.limit),
            order_by=[OrderBy(field="created_at", direction="desc")],
        )
        return _ok(page)

    name, entry = _tool(
        "find_sessions",
        (
            "Find sessions across every workspace by workspace, agent, "
            "state or initiator, newest first."
        ),
        (
            "Use when you manage sessions in OTHER workspaces, as the "
            "operator in prime does; not for listing one workspace's "
            "sessions (use ``list_workspace_sessions``). From a session "
            "outside the default workspace (prime) it finds only your own "
            "workspace's sessions."
        ),
        _FindSessionsArgs,
        _find_sessions,
        examples=[
            ToolExample(args={"workspace_id": "ws-1", "state": "running"}),
            ToolExample(args={"agent_id": "builder", "limit": 5}),
        ],
        required_role="user",
    )
    registry[name] = entry

    async def _get_session(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _WorkspaceSessionArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if not in_reach(await _caller_reach(ctx), args.workspace_id):
            # Out of the calling session's reach (review of #853): exactly as a workspace that does not exist (the registry's own words).
            return _err_from_primer(_unknown_workspace(args.workspace_id), error_type="not-found")
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
        info = await _overlay_row(await session.info(), args.workspace_id)
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
            "lifecycle state (running / waiting / paused / ended). The "
            "lifecycle fields (``status``, ``ended_reason``, "
            "``ended_detail``, ``ended_at``) are the durable session row's, "
            "the same answer as ``GET /v1/sessions/{id}``."
        ),
        (
            "Use when inspecting one session on a workspace; not for "
            "listing all of them (use ``list_workspace_sessions``). " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
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

    async def _session_messages(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        # The supported transcript reader (A-22): the raw .state path is
        # refused to non-admins, so this is the same rule and the same
        # reader as GET /v1/sessions/{sid}/messages.
        from primer.api.routers.sessions import _read_workspace_turn_log

        try:
            args = _SessionMessagesArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        row = await storage_provider.get_storage(WorkspaceSession).get(args.session_id)
        # Out of the calling session's reach (review of #853) answers exactly as a session that does not exist.
        if row is None or row.workspace_id != args.workspace_id or not in_reach(await _caller_reach(ctx), args.workspace_id):
            return _err(
                f"Session {args.session_id!r} does not exist on "
                f"workspace {args.workspace_id!r}",
                error_type="not-found",
            )
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            if ws is None:
                raise NotFoundError(f"Workspace {args.workspace_id!r} is not available")
            state_path = getattr(ws, "state_path", ".state")
            page = await _read_workspace_turn_log(
                workspace=ws,
                relative_path=f"{state_path}/sessions/{args.session_id}/messages.jsonl",
                limit=args.limit,
                offset=args.offset,
                since_seq=args.after_seq,
                tail=args.tail,
                dedupe_legacy_user_input=True,
                fallback_created_at=(
                    row.created_at.isoformat() if row.created_at else None
                ),
                workspace_id=args.workspace_id,
                session_id=args.session_id,
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="unavailable")
        return _ok(page)

    name, entry = _tool(
        "read_workspace_session_messages",
        (
            "Read a session's recorded transcript (the messages log): "
            "``{items, total, offset, limit}``, each item a record "
            "``{seq, kind, payload, created_at}`` (user_input, "
            "assistant_token, tool_call, tool_result, done, error, ...). "
            "Same reader and access rule as ``GET /v1/sessions/{id}/messages`` "
            "(no ``visible`` fold; a workspace that is not available is "
            "``not-found`` where REST returns an empty page)."
        ),
        (
            "Use when you need what a session said or did, e.g. to poll "
            "for its reply with ``after_seq``; not for its lifecycle state "
            "(use ``get_workspace_session``). Never read "
            "``.state/sessions/<id>/messages.jsonl`` with "
            "``read_workspace_file``: raw ``.state`` reads are refused "
            "unless the caller is an admin. " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
        ),
        _SessionMessagesArgs,
        _session_messages,
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "session_id": "sess-1", "after_seq": 4},
                returns="{items: [records with seq > 4], total, offset, limit}",
            ),
        ],
        required_role="user",
    )
    registry[name] = entry

    def _session_op(op_name: str, *, ranked: bool = False) -> ToolHandler:
        async def _handler(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
            try:
                args = _WorkspaceSessionArgs.model_validate(arguments)
            except ValidationError as exc:
                return _err_from_validation(exc)
            if not in_reach(await _caller_reach(ctx), args.workspace_id):
                # Out of the calling session's reach (review of #853): exactly as a workspace that does not exist (the registry's own words).
                return _err_from_primer(_unknown_workspace(args.workspace_id), error_type="not-found")
            if ranked:
                # A resume sets a run going at its creator's rank: the caller must out-rank or equal the creator (review of #853).
                target = await _session_row_storage().get(args.session_id)
                if target is not None and target.workspace_id == args.workspace_id:
                    refusal = _refuse_outranked(ctx, target)
                    if refusal is not None:
                        return refusal
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
            "lifecycle state (already ended, etc.). " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
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
            "the session is not currently paused. " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does. type=forbidden when the session runs at a higher rank than you (or your run)."
        ),
        _WorkspaceSessionArgs,
        _session_op("request_resume", ranked=True),
        examples=[
            ToolExample(
                args={"workspace_id": "ws-1", "session_id": "sess-1"},
                returns="{ok: true, session_id: ...}",
            ),
        ],
        required_role="user",
    )
    registry[name] = entry

    async def _send_from_session(
        ctx: ToolContext, sender_session_id: str, **options: Any,
    ) -> PeerSendResult:
        """:func:`send_peer_message` from the session that made this tool call (``message_session``, and ``steer_workspace_session`` called by an agent).

        WHO sends is the calling run's and nowhere else: the session is ``sender_session_id`` (the caller's ``ctx.session_id``, checked by the tool)
        and the principal is ``run_principal(ctx)``, so a run whose context names no initiator sends as an ordinary user, not as an unknown sender
        (which the service refuses). Neither is ever an argument a model chose. ``options`` are the service's own (``target_session_id``, ``text``...).
        """
        return await send_peer_message(
            storage_provider=storage_provider,
            scheduler=scheduler,
            claim_engine=claim_engine,
            workspace_registry=workspace_registry,
            event_bus=event_bus,
            sender_session_id=sender_session_id,
            sender_principal=run_principal(ctx),
            **options,
        )

    async def _steer_session(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        if scheduler is None or claim_engine is None:
            return _err(
                "session tools unavailable: scheduler/claim_engine not wired",
                error_type="unavailable",
            )
        try:
            args = _SteerArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        from primer.events.recorder import recorder_for
        from primer.session.steer_delivery import DELIVERED_MISSING, DELIVERED_QUEUED, deliver_steer

        sessions = storage_provider.get_storage(WorkspaceSession)
        row = await sessions.get(args.session_id)
        if row is None or row.workspace_id != args.workspace_id:
            return _err(_session_not_on_workspace(args), error_type="not-found")
        if ctx is not None and ctx.session_id:
            # An AGENT in a workspace session is steering another session (pivot P3, lead ruling 3): that is a message from the calling session, so it
            # goes through the service ``message_session`` uses and gets its guards (who sent it, the hop count, the per-sender rate limit, the rank
            # check) and the rulings keyed on a peer message (it never evicts a person's queued steer; it never reopens a session an operator
            # cancelled). A call with no calling session (the MCP endpoint, a chat) is a person or an API client steering, and takes the path below.
            # The named workspace is checked first (above), and then the reach of the caller's (lead ruling 3 of the G7 fixes): a session outside the
            # default workspace reaches only its own, and a session out of reach answers exactly as one that does not exist. It announces
            # ``session.steered`` for every delivery, as it always did.
            try:
                sent = await _send_from_session(
                    ctx,
                    ctx.session_id,
                    target_session_id=args.session_id,
                    text=args.instruction,
                    steered_on_every_delivery=True,
                )
            except PeerMessageRefused as exc:
                if exc.error_type == "not-found":
                    # Out of the caller's reach, or gone since the read above: in the words of a session that does not exist.
                    return _err(_session_not_on_workspace(args), error_type="not-found")
                return _err(exc.message, error_type=exc.error_type)
            after = await sessions.get(args.session_id) or row
            return _ok({**after.model_dump(mode="json"), "delivery": sent.delivery})
        # A person or an API client steering: they must rank at least as high as the session's creator (G7 ruling 2).
        refusal = _refuse_outranked(ctx, row)
        if refusal is not None:
            return refusal
        # The same delivery path as the REST steer route and every other machine entrypoint (triggers, channel replies): a session with a
        # turn open (armed, running, or parked on any gate) QUEUES the instruction for its next turn boundary instead of writing a second
        # user message, a parked gate is never answered by it, and an idle or ended session starts its next turn with it.
        delivery = await deliver_steer(
            session_id=args.session_id,
            text=args.instruction,
            parallelism="queue",
            # 01a08c08: this is an agent's tool call steering ANOTHER
            # session -- no human is behind it. Must not silently
            # clear an operator's pause on the target session.
            human_intent=False,
            storage_provider=storage_provider,
            scheduler=scheduler,
            claim_engine=claim_engine,
            workspace_registry=workspace_registry,
            event_bus=event_bus,
        )
        if delivery.outcome == DELIVERED_MISSING:
            # deliver_steer reports a session that cannot take a message as missing, and says why: an ended session that cannot be reopened (a
            # conflict), or a row that went away or a workspace that was removed between the read above and the delivery (not found, in the words
            # wake_session used, "workspace ... was removed while writing ...", which say what happened better than "does not exist").
            if delivery.conflict:
                current = await sessions.get(args.session_id)
                if current is not None and current.status == SessionStatus.ENDED:
                    message = f"Session {args.session_id!r} has ended ({current.ended_reason}) and cannot take a message"
                else:
                    message = delivery.detail or f"Session {args.session_id!r} cannot take a message"
                return _err(message, error_type="conflict")
            return _err(delivery.detail or _session_not_on_workspace(args), error_type="not-found")
        how = "queued" if delivery.outcome == DELIVERED_QUEUED else "woken"
        await recorder_for(storage_provider, event_bus).emit(
            "session.steered",
            actor=_caller_actor(ctx),
            workspace_id=row.workspace_id,
            session_id=row.id,
            payload={
                "has_instruction": True,
                "has_tool_results": False,
                "has_attachments": False,
                "delivery": how,
            },
        )
        after = await sessions.get(args.session_id) or row
        return _ok({**after.model_dump(mode="json"), "delivery": how})

    name, entry = _tool(
        "steer_workspace_session",
        (
            "Send a user instruction to a session: an idle or "
            "ended session starts its next turn with it, a busy, parked or "
            "paused one reads it at its next turn boundary."
        ),
        (
            "Use when you want to redirect a session; not to pause or resume "
            "it (use pause_workspace_session / resume_workspace_session) and "
            "not to answer its ask_user (a queued instruction is never an "
            "answer). Returns the session row plus delivery: woken or queued. "
            "From inside a workspace session it is a message from that "
            "session: the target sees a user message that starts with "
            "[from session <your session id> (agent <your agent id>)], and "
            "the limits of message_session apply (not-found outside your "
            "reach: a session outside the default workspace reaches only "
            "its own workspace; forbidden, hop-limit, rate-limited, "
            "conflict). With no calling session (MCP) any workspace is "
            "reachable, the caller must rank at least as high as the "
            "session's creator (type=forbidden), and a session an operator "
            "cancelled is refused (type=conflict)."
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
                returns='the session row plus {delivery: "woken"}',
            ),
        ],
        required_role="user",
        interruptible=False,
    )
    registry[name] = entry

    async def _restart_session(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        if scheduler is None or claim_engine is None:
            return _err(
                "session tools unavailable: scheduler/claim_engine not wired",
                error_type="unavailable",
            )
        try:
            args = _RestartArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        from primer.session.enqueue import SessionWakeDeps
        from primer.session.reset import SessionResetDeps, restart_session

        if ctx is not None and ctx.session_id:
            # An AGENT in a workspace session is restarting another session (lead ruling 1 of the G7 fixes): that is a message from the calling
            # session, exactly as a steer from it is, so it goes through the service ``message_session`` uses and gets every one of its guards (the
            # reach of the caller's workspace, the rank, the hop count, the per-sender rate limit) and its attribution (the prefix, ``payload.peer``,
            # the hop count of the reopened turn, the ledger row, ``session.messaged``). A session an operator cancelled is refused, as for any peer
            # message. A call with no calling session (the MCP endpoint) takes the path below.
            if args.input is None:
                return _err(REFUSE_RESTART_NEEDS_INPUT.format(limit=f"{PEER_MESSAGE_MAX_CHARS:,}"), error_type="bad-request")
            sessions = storage_provider.get_storage(WorkspaceSession)
            row = await sessions.get(args.session_id)
            if row is None or row.workspace_id != args.workspace_id:
                return _err(_session_not_on_workspace(args), error_type="not-found")
            try:
                await _send_from_session(
                    ctx, ctx.session_id, target_session_id=args.session_id, text=args.input, restart=True,
                )
            except PeerMessageRefused as exc:
                if exc.error_type == "not-found":
                    # Out of the caller's reach, or gone since the read above: answered in the words of a session that does not exist, so a
                    # session id seen in a shared file or a log is never confirmed.
                    return _err(_session_not_on_workspace(args), error_type="not-found")
                return _err(exc.message, error_type=exc.error_type)
            after = await sessions.get(args.session_id) or row
            return _ok(after.model_dump(mode="json"))

        # A person or an API client restarting: they must rank at least as high as the session's creator (G7 ruling 2). A missing session is the
        # not-found restart_session raises, in its own words.
        existing = await storage_provider.get_storage(WorkspaceSession).get(args.session_id)
        if existing is not None and existing.workspace_id == args.workspace_id:
            refusal = _refuse_outranked(ctx, existing)
            if refusal is not None:
                return refusal
        try:
            row = await restart_session(
                workspace_id=args.workspace_id,
                session_id=args.session_id,
                instruction=args.input,
                # 01a08c08: no person stands behind a tool call (an MCP client, or a context with no session), so it does not clear a pause
                # and does not reopen a session an operator cancelled (lead ruling 1 of the G7 fixes; only the REST Restart does).
                human_intent=False,
                reset_deps=SessionResetDeps(
                    storage_provider=storage_provider,
                    workspace_registry=workspace_registry,
                    event_bus=event_bus,
                ),
                wake_deps=SessionWakeDeps(
                    storage_provider=storage_provider,
                    scheduler=scheduler,
                    claim_engine=claim_engine,
                    workspace_registry=workspace_registry,
                    event_bus=event_bus,
                ),
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except ConflictError as exc:
            return _err_from_primer(exc, error_type="conflict")
        return _ok(row.model_dump(mode="json"))

    name, entry = _tool(
        "restart_workspace_session",
        (
            "Re-open an ended session and re-invoke it on the same "
            "workspace. Works for completed, failed, or cancelled sessions "
            "(ended_reason set); appends an 'invocation N' divider to the "
            "same stream and returns the re-armed session. A session whose "
            "last model call failed on a transport error is NOT ended: it "
            "rests (status waiting, last_turn_error set) and is continued "
            "with a new instruction, not restarted."
        ),
        (
            "Use when you want to run an ended session again (optionally "
            "with new input) without creating a new one; not to steer a "
            "live session or to continue one that rested after a failed "
            "model call (use ``steer_workspace_session``). A session that "
            "ended cancelled is refused (type=conflict): only a person "
            "reopens it. From inside a workspace session the restart is a "
            "message from that session: input is required, the target sees "
            "a user message that starts with [from session <your session "
            "id> (agent <your agent id>)], and the limits of message_session "
            "apply (not-found outside your reach, forbidden, hop-limit, "
            "rate-limited, conflict). With no calling session (MCP) the "
            "caller must rank at least as high as the session's creator "
            "(type=forbidden)."
        ),
        _RestartArgs,
        _restart_session,
        examples=[
            ToolExample(
                args={
                    "workspace_id": "ws-1",
                    "session_id": "sess-1",
                    "input": "run again with the new config",
                },
                returns="the re-armed session {status:\"running\"}",
            ),
        ],
        required_role="user",
        interruptible=False,
    )
    registry[name] = entry

    # ------------------- Messaging between sessions (pivot P3) ---------
    async def _list_my_sessions(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        # WHICH workspace is the caller's, from its tool context and nowhere else: the tool has no workspace argument.
        if ctx is None or not ctx.session_id or not ctx.workspace_id:
            return _outside_a_workspace_session("list_my_sessions")
        try:
            args = _ListMySessionsArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        return _ok(
            await list_workspace_session_summaries(
                storage_provider,
                workspace_id=ctx.workspace_id,
                caller_session_id=ctx.session_id,
                state=args.state,
                limit=args.limit,
            )
        )

    name, entry = _tool(
        "list_my_sessions",
        "List the sessions in your own workspace with their state, so you can pick one to message.",
        (
            "Use when you need the ids, agents and states of the other sessions in your workspace, for example before message_session; "
            "not for another workspace (use list_workspace_sessions) and not for one session's transcript "
            "(use read_workspace_session_messages). Each item says what a parked session waits on (parked_on) and marks your own "
            "session with self: true."
        ),
        _ListMySessionsArgs,
        _list_my_sessions,
        examples=[
            ToolExample(
                args={},
                returns=(
                    "{workspace_id, items: [{id, title, agent_id, graph_id, status, state, turn_status, parked_on, created_by, "
                    "parent_session_id, last_activity_at, self}], total}"
                ),
            ),
            ToolExample(args={"state": "running", "limit": 10}),
        ],
        requires_workspace=True,
        required_role="user",
    )
    registry[name] = entry

    async def _message_session(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        if ctx is None or not ctx.session_id or not ctx.workspace_id:
            return _outside_a_workspace_session("message_session")
        if scheduler is None or claim_engine is None:
            return _err(
                "session tools unavailable: scheduler/claim_engine not wired",
                error_type="unavailable",
            )
        try:
            args = _MessageSessionArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            result = await _send_from_session(
                ctx, ctx.session_id, target_session_id=args.session_id, text=args.text, mode=args.mode,
            )
        except PeerMessageRefused as exc:
            return _err(exc.message, error_type=exc.error_type)
        return _ok(result.model_dump(mode="json"))

    name, entry = _tool(
        "message_session",
        "Send a message to another session: an idle session starts its next turn with it, a busy one reads it at its next turn boundary.",
        (
            "Use when another session's agent should act on something or learn a result; not for large outputs "
            "(write a file and send its path) and not to answer a session's ask_user (a message is never an answer). "
            "The target sees a user message that starts with [from session <your session id> (agent <your agent id>)]. "
            "Refusals: bad-request (yourself), not-found (no such session in your workspace), forbidden (it runs at a higher rank "
            "than you), hop-limit (a chain of 8 messages), rate-limited (20 messages per 10 minutes), "
            "conflict (mode if_idle and the session is busy, or an ended session that cannot reopen)."
        ),
        _MessageSessionArgs,
        _message_session,
        examples=[
            ToolExample(
                args={"session_id": "ses-2", "text": "The API spec is in docs/api.md; build the client against it."},
                returns=(
                    '{message_id, session_id, workspace_id, delivery: "woken", hops: 1, target_state, target_parked_on, restarts_graph, note}'
                ),
            ),
            ToolExample(args={"session_id": "ses-3", "text": "Pick up the next ticket when you are free.", "mode": "if_idle"}),
        ],
        requires_workspace=True,
        required_role="user",
        # The delivery (several writes), then the ledger row, then two events, with no transaction: see tests/toolset/test_interruptible_declarations.py.
        interruptible=False,
    )
    registry[name] = entry

    # ------------------- Files sub-resource ---------------------------
    async def _list_files(
        arguments: dict[str, Any], ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        try:
            args = _ListFilesArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        refusal = await _out_of_reach(ctx, args.workspace_id)
        if refusal is not None:
            return refusal
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            refused = _refuse_reserved_read(ws, args.path, ctx)
            if refused is not None:
                return refused
            # 01a0645c: recursive=True used to walk the ENTIRE subtree
            # before this call got a chance to slice it - same cost risk
            # the files route (01a0644b, above in this same PR) already
            # fixed, but a naive copy of that fix would silently mislead
            # here: this tool's "total" field is agent-facing, persisted
            # into the session transcript, and read back on resume/
            # replanning - a `"total": 200` that's actually a capped
            # partial count (because the walk stopped early) could lead
            # the model to reason "small tree, safe to read everything"
            # about a workspace that's actually huge. Ruling: cap the
            # walk (bounded cost, same max_entries mechanism) AND be
            # honest about it - request one MORE entry than the cap so a
            # truncated walk is detected precisely (not guessed from
            # `len(entries) == cap`, which would also misfire when the
            # tree's true size happens to equal the cap exactly), then
            # only add the truncated/note fields when it actually fires
            # - every workspace under the cap gets byte-identical output
            # to before this fix.
            walk_cap = (
                min(args.offset + args.limit, _MAX_RECURSIVE_WALK_ENTRIES)
                if args.recursive else None
            )
            entries = await ws.list_files(
                args.path, recursive=args.recursive,
                max_entries=(walk_cap + 1) if walk_cap is not None else None,
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except (BadRequestError, PrimerError) as exc:
            return _err_from_primer(exc, error_type="bad-request")
        truncated = walk_cap is not None and len(entries) > walk_cap
        if truncated:
            entries = entries[:walk_cap]
        if not _caller_is_admin(ctx):
            # A recursive walk from the root descends into .state / .tmp.
            trees = reserved_trees(ws)
            entries = [e for e in entries if reserved_tree(e.path, trees) is None]
        sliced = entries[args.offset : args.offset + args.limit]
        result: dict[str, Any] = {
            "items": [e.model_dump(mode="json") for e in sliced],
            "offset": args.offset,
            "length": len(sliced),
            "total": len(entries),
            "path": args.path,
        }
        if truncated:
            result["truncated"] = True
            result["note"] = (
                f"the recursive walk was capped at {walk_cap} entries; "
                "the workspace tree may be larger than `total` reflects "
                "- narrow `path` or page further with `offset` to see more"
            )
        return _ok(result)

    name, entry = _tool(
        "list_workspace_files",
        (
            "List files in a workspace at ``path`` (default: root) "
            "with pagination. ``recursive=true`` walks the whole tree. "
            "Each item is a FileEntry (path, kind, size_bytes, "
            "modified_at). On a very large tree, the walk itself is "
            "capped for cost - the response then carries "
            "``truncated: true`` and a ``note``, and ``total`` is a "
            "lower bound, not necessarily the tree's true size."
        ),
        (
            "Use when browsing a workspace's filesystem; not for one "
            "entry's metadata (use ``get_workspace_file_info``) or its "
            "bytes (use ``read_workspace_file``). " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
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

    async def _file_info(
        arguments: dict[str, Any], ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        try:
            args = _WorkspacePathArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        refusal = await _out_of_reach(ctx, args.workspace_id)
        if refusal is not None:
            return refusal
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            refused = _refuse_reserved_read(ws, args.path, ctx)
            if refused is not None:
                return refused
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
            "path-escape attempts. " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
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

    async def _read_file(
        arguments: dict[str, Any], ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        try:
            args = _ReadFileArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if args.encoding not in ("text", "base64"):
            return _err(
                f"unknown encoding {args.encoding!r}; use 'text' or 'base64'",
                error_type="bad-request",
            )
        refusal = await _out_of_reach(ctx, args.workspace_id)
        if refusal is not None:
            return refusal
        try:
            ws = await workspace_registry.get_workspace(args.workspace_id)
            refused = _refuse_reserved_read(ws, args.path, ctx)
            if refused is not None:
                return refused
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
            "``write_workspace_file``). " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
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

    async def _delete_file(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _WorkspacePathArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        refusal = await _out_of_reach(ctx, args.workspace_id)
        if refusal is not None:
            return refusal
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
            "``.state`` / ``.tmp`` with ``type=bad-request``. " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
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
        # LocalWorkspace deletes in a thread under the scope lock: cancelling the await would release the lock while the
        # thread still deletes (the hazard of the write and edit tools). A Stop waits for the call.
        interruptible=False,
    )
    registry[name] = entry

    async def _write_file(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
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
        refusal = await _out_of_reach(ctx, args.workspace_id)
        if refusal is not None:
            return refusal
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
            "/ ``.tmp`` trees. " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
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
        interruptible=False,   # a thread write under the scope lock, as for delete_workspace_file above
    )
    registry[name] = entry

    # ------------------- Log sub-resource ----------------------------
    async def _get_log(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _LogArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        refusal = await _out_of_reach(ctx, args.workspace_id)
        if refusal is not None:
            return refusal
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
            "``get_workspace_session``). " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
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
    async def _workspace_tap(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
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
                "invalid selector: " + json.dumps(without_input(exc.errors()), default=str),
                error_type="bad-request",
            )
        # A LIKE pattern over the cap (G6-4) is refused before anything is read.
        try:
            check_selector(selector)
        except TapSelectorError as exc:
            return _err(f"invalid selector: {exc}", error_type="bad-request")

        # Tolerant decode: absent / empty / garbage → fresh drain.
        cursor = TapCursor.decode(args.cursor)

        # A workspace out of the calling session's reach answers as one that does not exist: the tap reads every session's stream in it.
        refusal = await _out_of_reach(ctx, args.workspace_id)
        if refusal is not None:
            return refusal

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

        try:
            events = await _drain()
        except TapSelectorTimeout as exc:
            # The selector's LIKE patterns ran out of the read's time budget
            # (G6-4): a clear error result; the cursor is not advanced.
            return _err(str(exc), error_type="bad-request")

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
            try:
                events = await _drain()
            except TapSelectorTimeout as exc:
                return _err(str(exc), error_type="bad-request")

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
            "lifecycle state (use ``get_workspace_session``). " + "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an unknown id does."
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
