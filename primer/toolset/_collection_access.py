"""Collection access for the tools that touch collections and their documents (access-control plan P3 Task 2, Decision 6).

The tool twin of the REST ``_require_collection``: ``read`` reads, lists and searches a collection's documents, ``full``
writes them, ``owner`` deletes the collection, shares it and changes its search. A collection the caller holds no level on
answers exactly like a missing one (``type=not-found``); too low a level answers ``type=forbidden``; any write to a
``system`` collection is refused (``type=forbidden``), whoever asks. Documents carry no access of their own: they inherit
their collection's, and the lists of documents only reach the collections the caller can read.
"""

from __future__ import annotations

from typing import Any

from primer.access.person import Person
from primer.access.policy import readable_ids, require
from primer.model.access import AccessLevel
from primer.model.chat import ToolCallResult
from primer.model.collection import Collection, Document
from primer.model.except_ import AccessDeniedError, NotFoundError
from primer.model.storage import Predicate
from primer.storage.q import Q
from primer.toolset._helpers import err


async def collection_for(
    collections: Any,
    person: Person,
    collection_id: str,
    need: AccessLevel,
    *,
    what: str | None = None,
    dangling_ok: bool = False,
) -> tuple[Collection | None, ToolCallResult | None]:
    """Load ``collection_id`` and enforce ``need`` on it for ``person``: ``(row, None)``, or ``(None, refusal)``.

    ``what`` names the thing a refusal is about (a document inherits its collection's access, so a refused document read
    says the DOCUMENT does not exist). ``dangling_ok`` lets an admin through when the collection is gone (``(None, None)``),
    so a document row left behind by it can still be read or removed.
    """
    what = what or f"Collection {collection_id!r}"
    coll = await collections.get(collection_id)
    if coll is None:
        if dangling_ok and person.is_admin:
            return None, None
        return None, err(f"{what} does not exist", error_type="not-found")
    try:
        require(person, coll.access, need, what=what)
    except NotFoundError:
        return None, err(f"{what} does not exist", error_type="not-found")
    except AccessDeniedError as exc:
        return None, err(exc.message, error_type="forbidden")
    if need is not AccessLevel.READ and coll.system:
        return None, err(f"collection {collection_id!r} is system-owned and read-only", error_type="forbidden")
    return coll, None


async def readable_documents(collections: Any, person: Person) -> Predicate | None:
    """``collection_id IN <collections person can read>`` for a document list; ``None`` for admins (no filter)."""
    ids = await readable_ids(collections, person)
    return None if ids is None else Q(Document).where_in("collection_id", ids).build()


__all__ = ["collection_for", "readable_documents"]
