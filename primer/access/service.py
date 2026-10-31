"""Read and change who an entity is shared with, and find users to share with (access-control spec 6.2, 9).

The REST routes (:mod:`primer.api.routers.access`) and the system tools call these same functions, so the two doors can
never enforce different rules (spec anti-goal 2).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from primer.access.person import Person
from primer.access.policy import level_of, require
from primer.int.storage import Storage
from primer.model.access import Access, AccessLevel
from primer.model.common import dump_for_storage
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
    storage: Storage[Any], users: Storage[User], entity_id: str, person: Person, *, what: str, use_only: bool = False
) -> AccessView:
    """The sharing of ``entity_id``; needs read access (no access answers not found).

    For grantable infrastructure (``use_only``) a non-admin gets only ``your_level`` and ``public``: no owners, no grants, so a colleague's
    use grant cannot be enumerated. Admins see everything.
    """
    row = await _load(storage, entity_id, what)
    level = require(person, row.access, AccessLevel.READ, what=what)
    if use_only and not person.is_admin:
        return AccessView(owners=[], grants=[], public=row.access.public, public_tools=[], rev=0, your_level=level)
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
    after_write: Callable[[Any], Awaitable[None]] | None = None,
    use_only: bool = False,
    harness_exists: Callable[[str], Awaitable[bool]] | None = None,
) -> AccessView:
    """Replace the sharing of ``entity_id``; owners only. Writes the ``access`` block alone, guarded on its ``rev``.

    A row a harness installed (it has a ``harness_id``) is shared through its harness, never directly: 409 ``harness_managed``. That holds only while the
    harness row EXISTS (``harness_exists``; none given: it does): a dangling ``harness_id`` (the harness was deleted, as ``apply_uninstall(cascade=False)``
    leaves it) names nothing that could share the row, so the row is shared directly again.
    ``after_write`` runs with the written row once the write has landed (a harness fans its access out to what it installed).

    ``use_only`` is the shape of grantable infrastructure (a model profile, a provider; spec 6.1): no owners, only ``read`` grants (meaning
    *use*), no per-tool flags. Admins hold ``owner`` by role, so ``require(..., OWNER)`` leaves the edit to them alone; a grantee reads the
    view and anyone else answers not found. Harness inheritance does not apply to these rows.
    """
    row = await _load(storage, entity_id, what)
    require(person, row.access, AccessLevel.OWNER, what=what)
    harness_id = getattr(row, "harness_id", None)
    if harness_id and not use_only and (harness_exists is None or await harness_exists(harness_id)):
        raise ConflictError(f"{what} is managed by a harness (harness_managed); share the harness instead", code="harness_managed")
    current: Access = row.access
    if update.rev is not None and update.rev != current.rev:
        raise ConflictError(f"the sharing of {what} changed since rev {update.rev}; reload it")
    owners = list(dict.fromkeys(update.owners))
    if use_only:
        if owners:
            raise ValidationError(f"{what} is shared by use grants only; it has no owners")
        if any(level != "read" for level in update.grants.values()):
            raise ValidationError(f"{what} is shared by use grants only: a grant is 'read' (use)")
        if update.public_tools:
            raise ValidationError(f"{what} has no tools to make public")
    elif not owners:
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
    if after_write is not None:
        await after_write(written)
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
    "rewrite_keeping_access",
    "set_access",
    "update_keeping_access",
]


async def rewrite_keeping_access(storage: Storage[Any], entity: Any, *, what: str, conn: Any | None = None) -> Any:
    """Write every field of ``entity`` except ``access``, against the sharing the row holds NOW (a long operation's snapshot may be stale).

    The row's stored ``access.rev`` is read fresh and the patch is fenced on it; a sharing change in between retries on the new rev, so an
    owner's grant or revoke is never put back and a long-running writer (a harness sync) never fails because somebody shared the row.
    """
    dumped = dump_for_storage(entity)
    patch = {key: value for key, value in dumped.items() if key not in ("id", "access")}
    extra = {} if conn is None else {"conn": conn}
    for _ in range(5):
        current = await storage.get(entity.id, **extra)
        if current is None:
            raise NotFoundError(f"{what} does not exist")
        rev = current.access.rev
        guard = [rev, None] if rev == 0 else [rev]
        written = await storage.patch_if(entity.id, patch, where={("access", "rev"): guard}, **extra)
        if written is not None:
            return written
    raise ConflictError(f"the sharing of {what} kept changing while it was being edited; retry")


async def update_keeping_access(storage: Storage[Any], entity: Any, existing: Any, *, what: str) -> Any:
    """Write every field of ``entity`` except ``access``, guarded on the stored sharing's ``rev``.

    An update keeps the row's sharing as stored; a blind whole-row write from the snapshot ``existing`` would put back a grant
    an owner revoked between the read and the write. One ``patch_if`` of every other field, fenced on ``access.rev``: a sharing
    change in the gap answers ``ConflictError`` (409) and nothing is written. Same storage event as an update (CRUD ``updated``).
    """
    dumped = dump_for_storage(entity)
    patch = {key: value for key, value in dumped.items() if key not in ("id", "access")}
    rev = existing.access.rev
    guard = [rev, None] if rev == 0 else [rev]
    written = await storage.patch_if(entity.id, patch, where={("access", "rev"): guard})
    if written is None:
        raise ConflictError(f"the sharing of {what} changed while it was being edited; reload it")
    return written
