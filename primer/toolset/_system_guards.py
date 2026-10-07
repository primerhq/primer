"""Guards the system CRUD tools share with the REST routers (task 01a111d1, D5 phase 1).

``make_crud_router`` attaches declarative guards to every REST router: harness-managed rows are read-only, reserved bootstrap ids
cannot be created or deleted, and a row another entity still references cannot be deleted. The system toolset's generic
``create_/update_/delete_<entity>`` tools re-implemented the six verbs with none of them, so an agent could write what REST
refuses. :class:`CrudGuards` is the per-entity declaration the tool factory (:func:`primer.toolset._system_crud._crud_tools_for`)
consumes; the checks below return a ready ``ToolCallResult`` refusal, or ``None`` to carry on. The answers mirror REST:

==========================  ===============================  =====================
check                       REST                             tool ``error_type``
==========================  ===============================  =====================
create sets the managed id  422 ``managed_field_set``        ``bad-request``
create a reserved id        409 ``reserved_id``              ``conflict``
update a reserved id        403 ``reserved_id_protected``    ``forbidden``
update / delete managed     409 ``managed_entity``           ``conflict``
delete a reserved id        403 ``reserved_id_protected``    ``forbidden``
delete a referenced row     409 ``in_use_by``                ``conflict``
==========================  ===============================  =====================

One check is STRICTER than REST on purpose: an update that sets the managed field on a row that has none is refused too
(REST only looks at the stored row), because a tool must not claim a row for a harness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from primer.model.chat import ToolCallResult
from primer.model.storage import Op
from primer.model.tool_approval import ToolApprovalPolicy
from primer.storage.references import first_referencing_row
from primer.toolset._helpers import err as _err

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


@dataclass(frozen=True)
class ToolReference:
    """A child kind that blocks the deletion of its parent while any row of it references the parent's id.

    Mirrors :class:`primer.api.routers._references.ReferenceCheck`, with the child's model class in place of a Request-taking
    storage callable (a tool has no Request).
    """

    child_kind: str
    child_model: type
    child_field: str
    op: Op = Op.EQ
    error_code: str = "in_use_by"


@dataclass(frozen=True)
class CrudGuards:
    """What the REST router for one entity guards, declared for its system tools. All empty means "no guards"."""

    kind: str = "entity"
    managed_by_field: str | None = None
    reserved_create_ids: frozenset[str] = frozenset()
    reserved_update_ids: frozenset[str] = frozenset()
    reserved_delete_ids: frozenset[str] = frozenset()
    references: tuple[ToolReference, ...] = field(default_factory=tuple)


def refuse_create(guards: CrudGuards, entity: Any) -> ToolCallResult | None:
    if guards.managed_by_field is not None and getattr(entity, guards.managed_by_field, None) is not None:
        return _err(
            f"{guards.managed_by_field} is set by the system that manages a {guards.kind} and cannot be set here",
            error_type="bad-request",
        )
    if entity.id in guards.reserved_create_ids:
        return _err(
            f"id {entity.id!r} is reserved and cannot be created via the API (reserved: {sorted(guards.reserved_create_ids)})",
            error_type="conflict",
        )
    return None


def refuse_update(guards: CrudGuards, entity: Any, existing: Any) -> ToolCallResult | None:
    # Reserved rows are re-created from config on boot, so changing one desyncs the runtime from the bootstrap defaults (REST
    # 403 for workspace providers and templates; the provider routers allow updating a reserved row, so they declare none).
    if existing.id in guards.reserved_update_ids:
        return _err(f"id {existing.id!r} is a reserved {guards.kind} and cannot be updated", error_type="forbidden")
    managed = guards.managed_by_field
    if managed is None:
        return None
    current = getattr(existing, managed, None)
    if current is not None:
        return _err(
            f"this {guards.kind} is managed via {managed}={current!r}; update it through the managing system instead",
            error_type="conflict",
        )
    if getattr(entity, managed, None) is not None:
        return _err(
            f"{managed} is set by the system that manages a {guards.kind} and cannot be set by an update",
            error_type="bad-request",
        )
    return None


def refuse_delete_id(guards: CrudGuards, entity_id: str) -> ToolCallResult | None:
    """Checked BEFORE the row lookup, as REST's ``on_pre_delete_id``: a reserved id is protected whether or not a row exists."""
    if entity_id in guards.reserved_delete_ids:
        return _err(f"id {entity_id!r} is a reserved {guards.kind} and cannot be deleted", error_type="forbidden")
    return None


def refuse_delete(guards: CrudGuards, existing: Any) -> ToolCallResult | None:
    managed = guards.managed_by_field
    if managed is not None and getattr(existing, managed, None) is not None:
        return _err(
            f"this {guards.kind} is managed via {managed}={getattr(existing, managed)!r}; "
            "delete it through the managing system instead",
            error_type="conflict",
        )
    return None


async def refuse_delete_if_referenced(
    guards: CrudGuards, existing: Any, storage_provider: "StorageProvider",
) -> ToolCallResult | None:
    """The first child kind that still references ``existing`` blocks the delete, as REST's reference-block hook."""
    for reference in guards.references:
        child = await first_referencing_row(
            storage_provider.get_storage(reference.child_model),
            field=reference.child_field, op=reference.op, parent_id=existing.id,
        )
        if child is not None:
            return _err(
                f"{reference.error_code}: 1 {reference.child_kind}(s) reference {existing.id!r} (first: {child.id!r})",
                error_type="conflict",
            )
    return None


# The managed-row declarations for the two entities whose generic create/update tools are exposed by MORE than one toolset: the system
# toolset's table and the ``crud`` (builder) toolset's re-homed descriptors. One definition each, because the factory's default is
# no guards, so every caller has to pass them (a structural test requires ``guards=`` on every call of ``_crud_tools_for``).
AGENT_GUARDS = CrudGuards(kind="agent", managed_by_field="harness_id")
GRAPH_GUARDS = CrudGuards(kind="graph", managed_by_field="harness_id")


def toolset_guards() -> CrudGuards:
    """The Toolset declaration, shared by the system toolset's table and the python-toolset tools (``create_python_toolset``,
    ``update_python_toolset_source``), which write Toolset rows directly.

    A function, not a constant: the reserved ids live in ``provider_registry``, whose module imports toolsets, so importing it
    at module level here would be a cycle (the same reason ``system.py`` imports its constants inside the builder).

    The reserved set is every id a stored row may not take (the registry's built-in providers and the tool-manager scopes): the
    same set the toolset REST create hook refuses, so the tool and the route cannot drift.
    """
    from primer.api.registries.provider_registry import RESERVED_TOOLSET_ROW_IDS

    return CrudGuards(
        kind="toolset",
        managed_by_field="harness_id",
        reserved_create_ids=RESERVED_TOOLSET_ROW_IDS,
        references=(ToolReference("tool_approval_policy", ToolApprovalPolicy, "toolset_id"),),
    )


__all__ = [
    "AGENT_GUARDS",
    "GRAPH_GUARDS",
    "CrudGuards",
    "ToolReference",
    "refuse_create",
    "refuse_delete",
    "refuse_delete_id",
    "refuse_delete_if_referenced",
    "refuse_update",
    "toolset_guards",
]
