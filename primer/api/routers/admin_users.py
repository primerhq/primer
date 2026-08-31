"""Admin CRUD router for User (/v1/admin/users).

Operator-only account management for multi-user RBAC. Mounted at
``/v1/admin/users`` and gated by :func:`primer.api.deps.require_admin`
at include-router time (see
:func:`primer.api._app_routes._mount_routers`), so only ``role ==
"admin"`` callers reach these endpoints.

HAND-WRITTEN (not :func:`primer.api.routers._crud.make_crud_router`).
The generic CRUD factory validates the raw request body as a full
``User`` model, which means a caller has to supply ``id``,
``created_at``, and a pre-hashed ``password_hash`` up front — fine for
machine-to-machine provider CRUD, unworkable for a human operator
provisioning an account from a "create user" form (Task 12's admin
console posts ``{username, password, email, role, disabled}`` and
expects the server to mint the id / hash the password), and its
``response_model=User`` leaks ``password_hash`` (an argon2 PHC string)
in every response body. This router:

* Never accepts ``id`` / ``created_at`` / ``password_hash`` from the
  wire — the server mints the id (mirrors ``POST /v1/auth/register``'s
  ``f"user-{uuid.uuid4().hex[:12]}"`` scheme), stamps ``created_at``,
  and hashes any plaintext ``password`` via
  :func:`primer.auth.passwords.hash_password`.
* Never returns ``password_hash`` — every response is
  :class:`AdminUserOut`, which simply omits the field.
* Enforces the anti-lockout guard (see :func:`_is_protected_admin` /
  :func:`_count_protected_admins`) on PATCH and DELETE: refuses any
  mutation that would leave the platform with zero *enabled admins
  that still hold a password*. Returns 403 ``last_admin_protected``.
* Supports ``generate_password`` on create/PATCH as an alternative to a
  caller-supplied ``password``: the server mints a random temp
  password, hashes it, sets ``must_change_password``, and returns the
  plaintext exactly once as ``generated_password`` on the response.
  Powers both "temp password shown once" at provisioning and a
  one-click "force rotation" quick action on an existing row.
* Rejects duplicate usernames on create with 409
  ``user_already_exists``, mirroring the register route's uniqueness
  check.

CDC note: ``User`` has no CDC registration anywhere in the codebase —
only ``agent``, ``graph``, ``document``, ``toolset``, and ``collection``
are registered kinds (grep for ``register_cdc_kind`` /
``cdc_kind=``), and the previous ``make_crud_router`` call here was
made *without* a ``cdc_kind=`` argument. So there is no CDC side
effect that this rewrite needs to preserve.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from primer.api.deps import get_user_storage
from primer.auth.passwords import hash_password
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.user import User


logger = logging.getLogger(__name__)

admin_users_router = APIRouter(prefix="/admin/users", tags=["admin-users"])

# Mirrors primer/api/routers/auth.py's RegisterBody rules — an
# operator-provisioned account is held to the same username shape /
# password strength as self-registration.
_USERNAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
_MIN_PASSWORD_LEN = 8

# Operator accounts are a small table; 200 is the storage layer's max
# page size (see OffsetPage.length). list/count loop across pages so
# behaviour stays correct even past that limit.
_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class AdminUserOut(BaseModel):
    """Response shape for every endpoint below. NEVER carries
    ``password_hash`` — that omission is the entire point of this
    hand-written router replacing ``make_crud_router``'s
    ``response_model=User``."""

    id: str
    username: str
    email: str | None = None
    role: Literal["admin", "user", "restricted"]
    disabled: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None = None
    generated_password: str | None = Field(
        default=None,
        description=(
            "Set only on the response to a create/PATCH call made with "
            "generate_password=true — the plaintext temp password, shown "
            "exactly once. Never persisted and never returned again."
        ),
    )

    @classmethod
    def from_user(
        cls, user: User, *, generated_password: str | None = None,
    ) -> "AdminUserOut":
        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
            disabled=user.disabled,
            must_change_password=user.must_change_password,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            generated_password=generated_password,
        )


class AdminUserListResponse(BaseModel):
    items: list[AdminUserOut]


class AdminUserCreateBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str | None = Field(
        default=None,
        min_length=_MIN_PASSWORD_LEN,
        description=(
            "Plaintext, hashed server-side. Omit (and leave "
            "generate_password false) to provision a password-less "
            "account (e.g. SSO-only, not yet activated)."
        ),
    )
    generate_password: bool = Field(
        default=False,
        description=(
            "Server mints a random temp password instead of the caller "
            "supplying one; returned once as generated_password on the "
            "response. Mutually exclusive with password."
        ),
    )
    email: str | None = None
    role: Literal["admin", "user", "restricted"] = "user"
    disabled: bool = False


class AdminUserUpdateBody(BaseModel):
    """All fields optional — PATCH semantics. Only keys actually present
    in the JSON body are applied (tracked via ``model_fields_set``), so
    omitting a key leaves it unchanged; an explicit ``"email": null``
    still clears it since that key *was* provided."""

    email: str | None = None
    role: Literal["admin", "user", "restricted"] | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=_MIN_PASSWORD_LEN)
    generate_password: bool = Field(
        default=False,
        description=(
            "Force-rotation quick action: server mints a random temp "
            "password, sets must_change_password, and returns it once as "
            "generated_password. Mutually exclusive with password."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_username(raw: str) -> str:
    return raw.strip().lower()


def _validate_username(name: str) -> None:
    if not _USERNAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_username",
                "message": "username must be 1–64 chars of [a-z 0-9 _ . -]",
            },
        )


async def _find_user_by_username(storage, username: str) -> User | None:
    page = await storage.find(
        Predicate(
            left=FieldRef(name="username"),
            op=Op.EQ,
            right=Value(value=username),
        ),
        OffsetPage(offset=0, length=1),
    )
    return page.items[0] if page.items else None


async def _get_or_404(storage, user_id: str) -> User:
    row = await storage.get(user_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "user_not_found",
                "message": f"user {user_id!r} does not exist",
            },
        )
    return row


def _is_protected_admin(user: User) -> bool:
    """True iff *user* counts toward the anti-lockout floor: an enabled
    admin that still holds a (non-null) password hash."""
    return (
        user.role == "admin"
        and not user.disabled
        and user.password_hash is not None
    )


async def _count_protected_admins(storage) -> int:
    """Number of enabled admins with a non-null password_hash, counted
    across ALL pages (fixes the previous ``make_crud_router``-era
    implementation's single-page(200) limitation — loops the same way
    :func:`primer.auth.bootstrap_admin.ensure_admin_exists` does, so the
    anti-lockout floor stays accurate past 200 users)."""
    count = 0
    offset = 0
    while True:
        page = await storage.list(OffsetPage(offset=offset, length=_PAGE_SIZE))
        count += sum(1 for u in page.items if _is_protected_admin(u))
        if len(page.items) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return count


def _raise_last_admin() -> None:
    raise HTTPException(
        status_code=403,
        detail={
            "error": "last_admin_protected",
            "message": (
                "refused: this change would leave no enabled admin with a "
                "password; promote or enable another admin first"
            ),
        },
    )


def _raise_duplicate_username(username: str) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "error": "user_already_exists",
            "message": f"a user named {username!r} already exists",
        },
    )


def _reject_conflicting_password_fields(
    *, password: str | None, generate_password: bool,
) -> None:
    if password and generate_password:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "conflicting_password_fields",
                "message": "supply either password or generate_password, not both",
            },
        )


def _generate_password() -> str:
    """A server-minted temp password, shown once in the response and
    never persisted in plaintext. token_urlsafe(15) yields a ~20-char
    base64url string, comfortably past _MIN_PASSWORD_LEN."""
    return secrets.token_urlsafe(15)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@admin_users_router.post(
    "",
    response_model=AdminUserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Provision a user account",
)
async def create_admin_user(
    body: AdminUserCreateBody,
    storage=Depends(get_user_storage),
) -> AdminUserOut:
    username = _normalise_username(body.username)
    _validate_username(username)
    _reject_conflicting_password_fields(
        password=body.password, generate_password=body.generate_password,
    )

    if await _find_user_by_username(storage, username) is not None:
        _raise_duplicate_username(username)

    plaintext = _generate_password() if body.generate_password else None
    pw_plain = body.password or plaintext
    pw_hash = await hash_password(pw_plain) if pw_plain else None
    user = User(
        id=f"user-{uuid.uuid4().hex[:12]}",
        username=username,
        password_hash=pw_hash,
        created_at=datetime.now(timezone.utc),
        email=body.email,
        role=body.role,
        disabled=body.disabled,
        # A password supplied (typed or generated) at provisioning time
        # forces a rotation on first login — the operator picked it, not
        # the end user.
        must_change_password=bool(pw_plain),
    )
    created = await storage.create(user)
    logger.info(
        "admin_users.create id=%s username=%s role=%s",
        created.id, created.username, created.role,
    )
    return AdminUserOut.from_user(created, generated_password=plaintext)


@admin_users_router.get(
    "",
    response_model=AdminUserListResponse,
    summary="List user accounts",
)
async def list_admin_users(
    storage=Depends(get_user_storage),
) -> AdminUserListResponse:
    rows: list[User] = []
    offset = 0
    while True:
        page = await storage.list(OffsetPage(offset=offset, length=_PAGE_SIZE))
        rows.extend(page.items)
        if len(page.items) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return AdminUserListResponse(items=[AdminUserOut.from_user(u) for u in rows])


@admin_users_router.get(
    "/{user_id}",
    response_model=AdminUserOut,
    summary="Get a user account by id",
)
async def get_admin_user(
    user_id: str = Path(..., description="User id."),
    storage=Depends(get_user_storage),
) -> AdminUserOut:
    row = await _get_or_404(storage, user_id)
    return AdminUserOut.from_user(row)


@admin_users_router.patch(
    "/{user_id}",
    response_model=AdminUserOut,
    summary="Update a user account (partial)",
)
async def update_admin_user(
    body: AdminUserUpdateBody,
    request: Request,
    user_id: str = Path(..., description="User id."),
    storage=Depends(get_user_storage),
) -> AdminUserOut:
    existing = await _get_or_404(storage, user_id)
    provided = body.model_fields_set
    _reject_conflicting_password_fields(
        password=body.password, generate_password=body.generate_password,
    )
    updated = existing.model_copy()

    if "email" in provided:
        updated.email = body.email
    if "role" in provided and body.role is not None:
        updated.role = body.role
    if "disabled" in provided and body.disabled is not None:
        updated.disabled = body.disabled

    plaintext = None
    if "generate_password" in provided and body.generate_password:
        plaintext = _generate_password()
        updated.password_hash = await hash_password(plaintext)
        updated.must_change_password = True
    elif "password" in provided and body.password:
        updated.password_hash = await hash_password(body.password)
        updated.must_change_password = True

    # Anti-lockout: only fires when *existing* currently counts as a
    # protected admin but *updated* would not. existing is still in
    # storage at this point so it is itself counted — a count of <= 1
    # means it is the sole protected admin.
    if _is_protected_admin(existing) and not _is_protected_admin(updated):
        if await _count_protected_admins(storage) <= 1:
            _raise_last_admin()

    saved = await storage.update(updated)
    logger.info("admin_users.update id=%s", saved.id)
    return AdminUserOut.from_user(saved, generated_password=plaintext)


@admin_users_router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a user account",
)
async def delete_admin_user(
    user_id: str = Path(..., description="User id."),
    storage=Depends(get_user_storage),
) -> None:
    existing = await _get_or_404(storage, user_id)
    if _is_protected_admin(existing):
        if await _count_protected_admins(storage) <= 1:
            _raise_last_admin()
    await storage.delete(user_id)
    logger.info("admin_users.delete id=%s", user_id)


__all__ = ["admin_users_router", "AdminUserOut", "AdminUserListResponse"]
