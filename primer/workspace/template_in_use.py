"""A workspace template that a workspace was made from cannot be deleted (lead ruling 6 of plan 08, platform revamp 2026-10).

A ``Workspace`` row names its template (``template_id``), and the name is LIVE, not a copy. A Docker or Kubernetes workspace is re-attached
on any process that has not cached it (after a restart, after a cache eviction, in a worker) by reading its template again
(``WorkspaceRegistry.get_workspace``, then the container and Kubernetes backends' ``get``). A template deleted while a workspace was made
from it therefore broke that workspace for good, although the template router and the tool both said the workspace would "keep working
without it" (e2e T0223 pinned that for a Local provider only, which never reads the template again).

So the delete is refused while any workspace references the template, whatever provider kind the template runs on, the way the provider
router already refuses a provider that a template or a workspace uses:

* REST: ``409`` with ``extensions.error`` ``template_in_use`` (``extensions.count`` and ``extensions.workspace_ids`` carry the numbers; the
  ``detail`` is the sentence);
* the ``workspaces::delete_workspace_template`` tool: ``type=conflict`` with the same sentence behind ``template_in_use:``.

The answer names how many workspaces use the template and the first :data:`MAX_WORKSPACE_IDS_SHOWN` ids, so a person knows what to delete
first. The route (``_workspace_template_pre_delete``) and the tool (``template_guards.delete_blockers``) both ask
:func:`workspaces_made_from`, so they cannot disagree about what blocks or what the answer says.

A workspace created while the delete runs can still slip in between the check and the delete, as it can for the provider guard: the
check is a read, not a lock. Snapshotting the template into the workspace row at create time would let a delete go through again; that
is a separate change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value

#: The ``extensions.error`` of the REST 409 and the lead of the tool's message.
TEMPLATE_IN_USE = "template_in_use"

#: How many workspace ids the refusal names; the count says how many there are in all.
MAX_WORKSPACE_IDS_SHOWN = 5

# A walk that has to count the rows past the ones it names asks for full pages of this many (the storage layer caps a page at 200).
_PAGE = 200


@dataclass(frozen=True)
class TemplateInUse:
    """The workspaces a template was used for: how many, and the first :data:`MAX_WORKSPACE_IDS_SHOWN` of them by id."""

    template_id: str
    count: int
    workspace_ids: tuple[str, ...]

    @property
    def message(self) -> str:
        """The sentence a person reads: what uses the template and what to do about it."""
        noun = "workspace" if self.count == 1 else "workspaces"
        ids = ", ".join(self.workspace_ids)
        listing = ids if self.count <= len(self.workspace_ids) else f"first {len(self.workspace_ids)}: {ids}"
        made = "the workspace made" if self.count == 1 else "the workspaces made"
        return f"Workspace template {self.template_id!r} is still used by {self.count} {noun} ({listing}). Delete {made} from it first."

    @property
    def detail(self) -> dict[str, Any]:
        """The ``detail`` of the REST 409: the problem's extensions, with the sentence as ``message``."""
        return {
            "error": TEMPLATE_IN_USE,
            "kind": "workspace_template",
            "count": self.count,
            "workspace_ids": list(self.workspace_ids),
            "message": self.message,
        }


async def workspaces_made_from(storage: Any, template_id: str) -> TemplateInUse | None:
    """The workspaces in ``storage`` (the ``Workspace`` store) whose ``template_id`` is ``template_id``, or ``None`` when none is.

    One query for the first :data:`MAX_WORKSPACE_IDS_SHOWN` rows by id, whose page also carries the total. A backend that does not supply a
    total (the storage contract allows it) is asked for the remaining rows page by page and they are counted.
    """
    match = Predicate(left=FieldRef(name="template_id"), op=Op.EQ, right=Value(value=template_id))
    page = await storage.find(match, OffsetPage(offset=0, length=MAX_WORKSPACE_IDS_SHOWN), order_by=[OrderBy(field="id", direction="asc")])
    if not page.items:
        return None
    shown = len(page.items)
    count = page.total if page.total is not None else await _count_from(storage, match, shown)
    return TemplateInUse(template_id=template_id, count=max(count, shown), workspace_ids=tuple(sorted(row.id for row in page.items)))


async def _count_from(storage: Any, match: Predicate, counted: int) -> int:
    """How many rows match in all, given that the first ``counted`` were already read (a full first page means there may be more)."""
    if counted < MAX_WORKSPACE_IDS_SHOWN:
        return counted
    total = counted
    while True:
        page = await storage.find(match, OffsetPage(offset=total, length=_PAGE), order_by=[OrderBy(field="id", direction="asc")])
        total += len(page.items)
        if len(page.items) < _PAGE:
            return total


__all__ = ["MAX_WORKSPACE_IDS_SHOWN", "TEMPLATE_IN_USE", "TemplateInUse", "workspaces_made_from"]
