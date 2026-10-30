"""Read and change who an entity is shared with, and find users to share with (access-control spec 6.2, 9).

The REST routes (:mod:`primer.api.routers.access`) and the system tools call these same functions, so the two doors can
never enforce different rules (spec anti-goal 2).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from primer.access.person import Person
from primer.access.policy import level_of, require
from primer.int.storage import Storage
from primer.model.access import Access, AccessLevel
from primer.model.except_ import AccessDeniedError, ConflictError, NotFoundError, ValidationError
from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.model.user import User

#: Upper bound of one user lookup page.
LOOKUP_MAX = 20


class UserRef(BaseModel):
    """A user as the sharing surfaces show them: id and username only, never the rest of the account."""

    id: str
    username: str | None = Field(default=None, description="None when the id names no user any more.")


class GrantView(BaseModel):
    user: UserRef
    level: Literal["read", "full"]


class AccessView(BaseModel):
    """An entity's sharing as its readers see it, plus the caller's own level."""

    owners: list[UserRef]
    grants: list[GrantView]
    public: bool
    public_tools: list[str]
    rev: int
    your_level: AccessLevel | None = Field(description="The caller's level; None after they gave their own access away.")


class AccessUpdate(BaseModel):
    """The whole desired sharing state. ``rev``, when given, must equal the current one (a stale editor gets 409)."""

    model_config = ConfigDict(extra="forbid")

    owners: list[str]
    grants: dict[str, Literal["read", "full"]] = {}
    public: bool = False
    public_tools: list[str] = []
    rev: int | None = None


async def _load(storage: Storage[Any], entity_id: str, what: str) -> Any:
    row = await storage.get(entity_id)
    if row is None:
        raise NotFoundError(f"{what} does not exist")
    return row


async def _user_refs(users: Storage[User], ids: list[str]) -> dict[str, UserRef]:
    refs: dict[str, UserRef] = {}
    for uid in ids:
        user = await users.get(uid)
        refs[uid] = UserRef(id=uid, username=user.username if user is not None else None)
    return refs


def _view(access: Access, refs: dict[str, UserRef], level: AccessLevel | None) -> AccessView:
    return AccessView(
        owners=[refs[uid] for uid in access.owners],
        grants=[GrantView(user=refs[uid], level=lvl) for uid, lvl in sorted(access.grants.items())],
        public=access.public,
        public_tools=list(access.public_tools),
        rev=access.rev,
        your_level=level,
    )


async def get_access_view(
    storage: Storage[Any], users: Storage[User], entity_id: str, person: Person, *, what: str
) -> AccessView:
    """The sharing of ``entity_id``; needs read access (no access answers not found)."""
    row = await _load(storage, entity_id, what)
    level = require(person, row.access, AccessLevel.READ, what=what)
    refs = await _user_refs(users, [*row.access.owners, *row.access.grants])
    return _view(row.access, refs, level)


async def set_access(
    storage: Storage[Any],
    users: Storage[User],
    entity_id: str,
    person: Person,
    update: AccessUpdate,
    *,
    what: str,
    allow_public_tools: bool = False,
) -> AccessView:
    """Replace the sharing of ``entity_id``; owners only. Writes the ``access`` block alone, guarded on its ``rev``."""
    row = await _load(storage, entity_id, what)
    require(person, row.access, AccessLevel.OWNER, what=what)
    current: Access = row.access
    if update.rev is not None and update.rev != current.rev:
        raise ConflictError(f"the sharing of {what} changed since rev {update.rev}; reload it")
    owners = list(dict.fromkeys(update.owners))
    if not owners:
        raise ValidationError("an entity needs at least one owner")
    if update.public_tools and not allow_public_tools:
        raise ValidationError(f"{what} has no tools to make public")
    refs: dict[str, UserRef] = {}
    for uid in dict.fromkeys([*owners, *update.grants]):
        user = await users.get(uid)
        if user is None or user.disabled or user.role == "restricted":
            raise ValidationError(f"{uid!r} is not a user this can be shared with")
        refs[uid] = UserRef(id=uid, username=user.username)
    new = Access(
        owners=owners,
        grants=update.grants,
        public=update.public,
        public_tools=list(dict.fromkeys(update.public_tools)),
        rev=current.rev + 1,
    )
    # A row written before ``rev`` existed has no stored leaf; the default 0 then matches an absent one.
    guard = [current.rev, None] if current.rev == 0 else [current.rev]
    written = await storage.patch_if(entity_id, {"access": new.model_dump(mode="json")}, where={("access", "rev"): guard})
    if written is None:
        raise ConflictError(f"the sharing of {what} changed meanwhile; reload it")
    return _view(written.access, refs, level_of(person, written.access))


def _escape_like(value: str) -> str:
    # The ILIKE renderers emit ESCAPE '\'; same rule as primer.api.routers._crud._escape_like.
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def lookup_users(users: Storage[User], person: Person, q: str, limit: int = 10) -> list[UserRef]:
    """Enabled, non-restricted users whose username starts with ``q`` (case-insensitive); full users only."""
    if not person.is_full_user:
        raise AccessDeniedError("looking up users needs a full account")
    if not q:
        return []
    clauses = [
        Predicate(left=FieldRef(name="username"), op=Op.ILIKE, right=Value(value=f"{_escape_like(q)}%")),
        Predicate(left=FieldRef(name="disabled"), op=Op.EQ, right=Value(value=False)),
        Predicate(left=FieldRef(name="role"), op=Op.NE, right=Value(value="restricted")),
    ]
    predicate = clauses[-1]
    for clause in reversed(clauses[:-1]):
        predicate = Predicate(left=clause, op=Op.AND, right=predicate)
    page = await users.find(
        predicate,
        OffsetPage(offset=0, length=max(1, min(limit, LOOKUP_MAX))),
        order_by=[OrderBy(field="username", direction="asc")],
    )
    return [UserRef(id=u.id, username=u.username) for u in page.items]


__all__ = [
    "LOOKUP_MAX",
    "AccessUpdate",
    "AccessView",
    "GrantView",
    "UserRef",
    "get_access_view",
    "lookup_users",
    "set_access",
]
