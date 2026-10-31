"""System tools for reading and changing who an entity is shared with, and finding users to share with (spec 6.2, 9).

The tool twins of ``GET`` / ``PUT /v1/{plural}/{id}/access`` and ``GET /v1/users/lookup``: they call the same
:mod:`primer.access.service` functions the routes call, so the two doors enforce identical rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from primer.access.person import person_for_tool
from primer.access.service import LOOKUP_MAX, AccessUpdate, get_access_view, lookup_users, set_access
from primer.model.agent import Agent
from primer.model.channel import Channel
from primer.model.collection import Collection
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import AccessDeniedError, ConflictError, NotFoundError
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.model.graph import Graph
from primer.model.providers.toolset import Toolset
from primer.model.trigger import Trigger
from primer.model.user import User
from primer.model.workspace import WorkspaceTemplate
from primer.model.yield_ import ToolContext
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err, ok
from primer.toolset._system_common import SYSTEM_TOOLSET_ID, _err_from_validation
from primer.toolset.internal import ToolHandler

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider

EntityKind = Literal["agent", "graph", "toolset", "workspace_template", "channel", "collection", "trigger"]

#: kind -> (model, label, allows per-tool public flags)
_KINDS: dict[str, tuple[type, str, bool]] = {
    "agent": (Agent, "Agent", False),
    "graph": (Graph, "Graph", False),
    "toolset": (Toolset, "Toolset", True),
    "workspace_template": (WorkspaceTemplate, "WorkspaceTemplate", False),
    "channel": (Channel, "Channel", False),
    "collection": (Collection, "Collection", False),
    "trigger": (Trigger, "Trigger", False),
}


class _GetAccessArgs(BaseModel):
    kind: EntityKind = Field(..., description="Which kind of entity.")
    id: str = Field(..., min_length=1, description="The entity's id.")


class _SetAccessArgs(AccessUpdate):
    kind: EntityKind = Field(..., description="Which kind of entity.")
    id: str = Field(..., min_length=1, description="The entity's id.")


class _LookupArgs(BaseModel):
    q: str = Field(..., max_length=64, description="Username prefix, case-insensitive.")
    limit: int = Field(default=10, ge=1, le=LOOKUP_MAX)


def _refusal(exc: Exception) -> ToolCallResult:
    if isinstance(exc, NotFoundError):
        return err(str(exc), error_type="not-found")
    if isinstance(exc, AccessDeniedError):
        return err(str(exc), error_type="forbidden")
    if isinstance(exc, ConflictError):
        return err(str(exc), error_type="conflict")
    return err(str(exc), error_type="validation-error")


def access_tools(*, storage_provider: "StorageProvider") -> dict[str, tuple[Tool, ToolHandler]]:
    """Build ``get_entity_access``, ``set_entity_access`` and ``lookup_users`` for the system toolset."""
    users = storage_provider.get_storage(User)

    async def _get(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _GetAccessArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        model, label, _ = _KINDS[args.kind]
        try:
            view = await get_access_view(
                storage_provider.get_storage(model), users, args.id, person_for_tool(ctx), what=f"{label} {args.id!r}"
            )
        except (NotFoundError, AccessDeniedError) as exc:
            return _refusal(exc)
        return ok(view.model_dump(mode="json"))

    async def _set(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _SetAccessArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        model, label, allow_public_tools = _KINDS[args.kind]
        update = AccessUpdate.model_validate(args.model_dump(exclude={"kind", "id"}))
        try:
            view = await set_access(
                storage_provider.get_storage(model),
                users,
                args.id,
                person_for_tool(ctx),
                update,
                what=f"{label} {args.id!r}",
                allow_public_tools=allow_public_tools,
            )
        except (NotFoundError, AccessDeniedError, ConflictError, PrimerValidationError) as exc:
            return _refusal(exc)
        return ok(view.model_dump(mode="json"))

    async def _lookup(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _LookupArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            found = await lookup_users(users, person_for_tool(ctx), args.q, args.limit)
        except AccessDeniedError as exc:
            return _refusal(exc)
        return ok([u.model_dump(mode="json") for u in found])

    get_tool = make_tool(
        id="get_entity_access",
        toolset_id=SYSTEM_TOOLSET_ID,
        purpose="Read who an agent, graph, toolset, workspace template, channel, collection or trigger is shared with.",
        when=(
            "Use when checking an entity's owners, the people it is shared with and at what level (read or full), whether it is "
            "public, and your own level. Needs read access; an entity you cannot read answers type=not-found."
        ),
        args_schema=_GetAccessArgs.model_json_schema(),
        examples=[
            ToolExample(
                args={"kind": "agent", "id": "agent-3f9a1c8d"},
                returns="``{owners, grants, public, public_tools, rev, your_level}``",
            ),
        ],
        required_role="user",
        preview_args=("kind", "id"),
    )
    set_tool = make_tool(
        id="set_entity_access",
        toolset_id=SYSTEM_TOOLSET_ID,
        purpose="Change who an agent, graph, toolset, workspace template, channel, collection or trigger is shared with (owners only).",
        when=(
            "Use when sharing an entity with other users, making it public, handing ownership to someone else, or marking single "
            "toolset tools public. Send the whole desired state (owners, grants, public, public_tools) and the rev you read; a stale "
            "rev answers type=conflict. Owners only (type=forbidden otherwise); at least one owner; only full, enabled users."
        ),
        args_schema=_SetAccessArgs.model_json_schema(),
        examples=[
            ToolExample(
                args={"kind": "agent", "id": "agent-3f9a1c8d", "owners": ["user-1"], "grants": {"user-2": "read"}, "rev": 0},
                returns="the new sharing, as get_entity_access returns it",
            ),
            ToolExample(
                args={"kind": "toolset", "id": "toolset-9", "owners": ["user-1"], "public_tools": ["lookup"], "rev": 2},
                returns="the new sharing with the one tool public",
            ),
        ],
        required_role="user",
        preview_args=("kind", "id", "owners", "grants", "public"),
    )
    lookup_tool = make_tool(
        id="lookup_users",
        toolset_id=SYSTEM_TOOLSET_ID,
        purpose="Find users to share with by username prefix.",
        when=(
            "Use when you know part of a person's username and need their user id for set_entity_access. Returns id and username "
            "only, for enabled full users."
        ),
        args_schema=_LookupArgs.model_json_schema(),
        examples=[ToolExample(args={"q": "ali"}, returns='``[{"id": "user-1", "username": "alice"}]``')],
        required_role="user",
        preview_args=("q",),
    )
    return {
        get_tool.id: (get_tool, _get),
        set_tool.id: (set_tool, _set),
        lookup_tool.id: (lookup_tool, _lookup),
    }


__all__ = ["access_tools"]
