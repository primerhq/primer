"""Pre-write checks for :class:`~primer.model.collection.Collection`, shared by the REST router and the system tools.

``Collection.system`` marks a collection the platform regenerates from its own state (the system map, the catalog, the internal
collections). The platform writes those rows straight to storage, never through the generic CRUD routes or the ``create_collection`` /
``update_collection`` tools, so the flag is not the client's to set: ``DELETE`` refuses a system collection, and while ``system`` was an
ordinary body field a client cleared it with a PUT and then deleted the collection, which the delete cascade empties.

The rule: a create may not set the flag, and an update may not change it (a body that omits it counts as clearing it, because the model
default is false). An update that leaves the flag as it was is fine, so a client that reads a row and writes it back keeps working. The
REST hooks answer 403 and the tools ``type=forbidden``, as for the other platform-owned rules.
"""

from __future__ import annotations

from primer.common.entity_checks import EntityCheckError
from primer.model.collection import Collection

SYSTEM_FLAG_PROTECTED = "system_flag_protected"


def check_collection_system_flag(entity: Collection, existing: Collection | None = None) -> None:
    """Refuse a client write that sets ``system`` on create (``existing`` is ``None``) or changes it on update."""
    if existing is None:
        if entity.system:
            raise EntityCheckError(
                "forbidden",
                "the system flag is set by the platform and cannot be set through the API; create the collection without it",
                code=SYSTEM_FLAG_PROTECTED,
            )
        return
    if entity.system != existing.system:
        kept = "a system collection stays one" if existing.system else "a user collection stays one"
        raise EntityCheckError(
            "forbidden",
            f"the system flag of collection {existing.id!r} cannot be changed through the API ({kept}; "
            "send the stored value, and note that leaving the field out means false)",
            code=SYSTEM_FLAG_PROTECTED,
        )


__all__ = ["SYSTEM_FLAG_PROTECTED", "check_collection_system_flag"]
