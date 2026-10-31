"""Which tools of which toolsets a person may use (access-control spec 6.9; plan P4 Task 2).

``toolset_use`` answers for a whole turn's toolsets with ONE batched read of the toolset rows. A toolset with no row (a built-in or reserved id) is open to
every full user. A row the person holds a level on gives every tool. Otherwise only the tools the row marks public (``public_tools``) are usable, so a row with
none usable is closed. Admins short-circuit with no read.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from primer.access.person import Person
from primer.access.policy import level_of
from primer.common.toolset_ids import canonical_toolset_id
from primer.model.provider import Toolset
from primer.model.user import User
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider

#: ``None`` means every tool of the toolset, a set means only those tools, an empty set means none.
ToolUse = frozenset[str] | None

_PAGE = 200


async def toolset_use(sp: "StorageProvider", person: Person, toolset_ids: Iterable[str]) -> dict[str, ToolUse]:
    """For each id in ``toolset_ids`` (as spelled), the tools ``person`` may use: ``None`` every tool, a set only those, an empty set none."""
    ids = list(dict.fromkeys(toolset_ids))
    if person.is_admin:
        return dict.fromkeys(ids)
    if not person.is_full_user:
        return {i: frozenset() for i in ids}
    canonical = list(dict.fromkeys(canonical_toolset_id(i) for i in ids))
    rows: dict[str, Toolset] = {}
    for start in range(0, len(canonical), _PAGE):
        chunk = canonical[start:start + _PAGE]
        page = await sp.get_storage(Toolset).find(
            Predicate(left=FieldRef(name="id"), op=Op.IN, right=Value(value=chunk)), OffsetPage(offset=0, length=_PAGE),
        )
        rows.update({r.id: r for r in page.items})
    out: dict[str, ToolUse] = {}
    for spelled in ids:
        row = rows.get(canonical_toolset_id(spelled))
        if row is None or level_of(person, row.access) is not None:
            out[spelled] = None
        else:
            out[spelled] = frozenset(row.access.public_tools)
    return out


async def may_use_toolset(sp: "StorageProvider", person: Person, toolset_id: str) -> bool:
    """Whether ``person`` may use at least one tool of the toolset (every tool, or a public one): who may sign in to it and read its status."""
    use = (await toolset_use(sp, person, [toolset_id]))[toolset_id]
    return use is None or bool(use)


async def user_may_use_toolset(sp: "StorageProvider", user_id: str, toolset_id: str) -> bool:
    """:func:`may_use_toolset` for the user ``user_id`` as stored NOW (not as a run or a pending sign-in remembered them): False when the user is
    gone, disabled or not a full user."""
    user = await sp.get_storage(User).get(user_id)
    if user is None or user.disabled or user.role not in ("user", "admin"):
        return False
    return await may_use_toolset(sp, Person(user_id, user.role), toolset_id)


__all__ = ["ToolUse", "may_use_toolset", "toolset_use", "user_may_use_toolset"]
