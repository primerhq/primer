"""The access policy: levels, enforcement and the list predicate (access-control spec 5.1, 6.2, 9).

``level_of`` decides what level a person holds on one row; ``require``
enforces a minimum level and turns refusals into the right errors (no
level at all: ``NotFoundError``, because an out-of-access read answers
exactly like a missing row; insufficient level: ``AccessDeniedError``).
``visible_predicate`` compiles the filter a person's lists run against
storage over ``access.members`` -- the GIN-indexed derived tokens (spec
anti-goal 4: filter in the predicate, never a fetched page in Python).
``tool_is_public`` is the per-tool public flag, for Phase 4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from primer.access.person import Person
from primer.model.access import Access, AccessLevel, LEVEL_RANK, TOOLS_PUBLIC_TOKEN
from primer.model.except_ import AccessDeniedError, NotFoundError
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value

if TYPE_CHECKING:
    from primer.int.storage import Storage

_MEMBERS = "access.members"
_PUBLIC_TOKEN = "public"
_NO_MATCH_TOKEN = "__none__"
#: ``OffsetPage.length`` is bounded to 200; the page size the id-set walk uses.
_PAGE_SIZE = 200


def _contains(value: str) -> Predicate:
    """``access.members`` CONTAINS ``value``: JSONB array membership, GIN-backed."""
    return Predicate(
        left=FieldRef(name=_MEMBERS), op=Op.CONTAINS, right=Value(value=value)
    )


def member_predicate(user_id: str) -> Predicate:
    """``access.members`` CONTAINS ``u:<user_id>``: the rows that name a user as an owner or a grantee (GIN-backed)."""
    return _contains(f"u:{user_id}")


def level_of(person: Person, access: Access) -> AccessLevel | None:
    """The level ``person`` holds on ``access``, or ``None`` for no access.

    Admins and the auth-disabled system principal hold ``owner`` on
    everything; a ``restricted`` person (the onboarding gate) holds
    nothing; then owner, then the person's grant, then ``public``
    (always ``read`` for every full user); otherwise nothing.
    """
    if person.is_admin:
        return AccessLevel.OWNER
    if not person.is_full_user:
        return None
    if person.user_id is not None:
        if person.user_id in access.owners:
            return AccessLevel.OWNER
        grant = access.grants.get(person.user_id)
        if grant is not None:
            return AccessLevel(grant)
    if access.public:
        return AccessLevel.READ
    return None


def require(person: Person, access: Access, need: AccessLevel, *, what: str) -> AccessLevel:
    """Enforce that ``person`` holds at least ``need`` on ``access``; return the held level.

    No level at all raises ``NotFoundError`` -- an out-of-access read
    must answer exactly like a missing row (spec 9), so callers cannot
    distinguish "exists but not yours" from "does not exist". A level
    below ``need`` raises ``AccessDeniedError``.
    """
    have = level_of(person, access)
    if have is None:
        raise NotFoundError(f"{what} does not exist")
    if LEVEL_RANK[have] < LEVEL_RANK[need]:
        raise AccessDeniedError(f"{what} needs {need} access")
    return have


def visible_predicate(person: Person) -> Predicate | None:
    """The storage predicate a person's list queries run against, or ``None`` for no filter.

    ``None`` for admins (they see everything). For a full user:
    ``access.members`` CONTAINS ``"u:<id>"`` OR CONTAINS ``"public"``
    (a person without a user id gets only the public clause, so they
    read exactly the public rows). For anyone else a predicate that
    matches nothing -- an empty list, never an unfiltered one.
    """
    if person.is_admin:
        return None
    if person.is_full_user:
        terms = []
        if person.user_id is not None:
            terms.append(_contains(f"u:{person.user_id}"))
        terms.append(_contains(_PUBLIC_TOKEN))
        combined = terms[-1]
        for term in reversed(terms[:-1]):
            combined = Predicate(left=term, op=Op.OR, right=combined)
        return combined
    return _contains(_NO_MATCH_TOKEN)


async def readable_ids(
    storage: "Storage[Any]", person: Person, need: AccessLevel = AccessLevel.READ
) -> list[str] | None:
    """The ids of the owned rows in ``storage`` that ``person`` holds at least ``need`` on.

    ``None`` for admins (they see everything: no filter). Otherwise the ids
    from a ``visible_predicate`` find, kept only while the held level reaches
    ``need`` (public rows count as read). The id set an inherited kind's
    lists filter on (``<parent>_id IN <ids>``).
    """
    if person.is_admin:
        return None
    predicate = visible_predicate(person)
    ids: list[str] = []
    offset = 0
    while True:
        items = (await storage.find(predicate, OffsetPage(offset=offset, length=_PAGE_SIZE))).items
        for row in items:
            held = level_of(person, row.access)
            if held is not None and LEVEL_RANK[held] >= LEVEL_RANK[need]:
                ids.append(row.id)
        if len(items) < _PAGE_SIZE:
            return ids
        offset += _PAGE_SIZE


def may_use_tool(person: Person, access: Access, tool_name: str) -> bool:
    """Whether ``person`` may use ``tool_name`` of a toolset with ``access``: a level on the row gives every tool; otherwise a full user may use the
    tools the row marks public (``public_tools``), exactly those and never their siblings."""
    if level_of(person, access) is not None:
        return True
    return person.is_full_user and tool_name in access.public_tools


def catalogue_predicate(person: Person) -> Predicate | None:
    """The predicate a tool catalogue runs against: the rows the person can see, plus the rows that have public tools (``tools:public``).

    ``None`` for admins. The caller shows only the public tools of a row it holds no level on.
    """
    if person.is_admin:
        return None
    visible = visible_predicate(person)
    if not person.is_full_user:
        return visible
    return Predicate(left=visible, op=Op.OR, right=_contains(TOOLS_PUBLIC_TOKEN))


def tool_is_public(access: Access, tool_name: str) -> bool:
    """Whether ``tool_name`` is publicly readable on ``access`` (Phase 4: tool-level public)."""
    return access.public or tool_name in access.public_tools
