"""Use grants on infrastructure (access-control P4 Task 3, spec 6.1, 6.2).

Model profiles and the providers a user-owned entity references carry an admin-set use grant. ``usable`` is the one question every
check asks (the saver of an entity that names the row, the person a run acts as): it answers the row, or ``None`` when the row is missing
or the person holds no level on it, so an unusable row reads exactly like a missing one (spec 9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from primer.access.person import Person
from primer.access.policy import level_of

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


async def usable(sp: "StorageProvider", person: Person, model_cls: type, row_id: str) -> Any | None:
    """The ``model_cls`` row ``row_id`` when ``person`` may use it, else ``None`` (missing, or no use grant and not public).

    Admins and the auth-disabled system principal use everything; a restricted person uses nothing.
    """
    row = await sp.get_storage(model_cls).get(row_id)
    if row is None or level_of(person, row.access) is None:
        return None
    return row


__all__ = ["usable"]
