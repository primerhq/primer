"""Admin CRUD router for User (/v1/admin/users).

Operator-only account management for multi-user RBAC. Mounted at
``/v1/admin/users`` and gated by :func:`primer.api.deps.require_admin`
at include-router time (see
:func:`primer.api._app_routes._mount_routers`), so only ``role ==
"admin"`` callers reach these endpoints.

Built on :func:`primer.api.routers._crud.make_crud_router` (same shape as
the provider routers) plus three guards:

* **create** — when the body carries a ``password_hash`` the row is forced
  ``must_change_password = True`` so an operator-provisioned account must
  rotate the password on first login.
* **update / delete** — an anti-lockout guard refuses any mutation that
  would leave the platform with zero *enabled admins that still hold a
  password* (delete / demote / disable / password-clear of the last such
  admin). Returns 403 ``last_admin_protected``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from primer.api.deps import get_user_storage
from primer.api.routers._crud import make_crud_router
from primer.model.storage import OffsetPage
from primer.model.user import User


def _user_storage(request: Request):
    """Storage[User] off app.state — used inside the guard hooks."""
    return request.app.state.storage_provider.get_storage(User)


def _is_protected_admin(user: User) -> bool:
    """True iff *user* counts toward the anti-lockout floor: an enabled
    admin that still holds a (non-null) password hash."""
    return (
        user.role == "admin"
        and not user.disabled
        and user.password_hash is not None
    )


async def _count_protected_admins(storage) -> int:
    """Number of enabled admins with a non-null password_hash.

    Operator accounts are a small table, so a single generous page is
    enough; 200 is the storage layer's max page size.
    """
    page = await storage.list(OffsetPage(offset=0, length=200))
    return sum(1 for u in page.items if _is_protected_admin(u))


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


async def _on_pre_create(entity: User, request: Request) -> None:
    """Force must_change_password when the admin provisions a password."""
    if entity.password_hash is not None:
        entity.must_change_password = True


async def _on_pre_update(entity: User, existing: User, request: Request) -> None:
    """Refuse demote / disable / password-clear of the last protected admin.

    The guard only fires when *existing* currently counts as a protected
    admin but the incoming *entity* would not. Because *existing* is still
    in storage at hook time it is itself counted, so a count of ``<= 1``
    means it is the sole protected admin.
    """
    if _is_protected_admin(existing) and not _is_protected_admin(entity):
        storage = _user_storage(request)
        if await _count_protected_admins(storage) <= 1:
            _raise_last_admin()


async def _on_pre_delete(existing: User, request: Request) -> None:
    """Refuse deleting the last protected admin."""
    if _is_protected_admin(existing):
        storage = _user_storage(request)
        if await _count_protected_admins(storage) <= 1:
            _raise_last_admin()


# ``plural="admin/users"`` makes the CRUD routes land at
# ``/admin/users[...]`` which, combined with the ``/v1`` include prefix,
# yields ``/v1/admin/users``.
admin_users_router = make_crud_router(
    model_cls=User,
    storage_dep=get_user_storage,
    plural="admin/users",
    tag="admin-users",
    on_pre_create=_on_pre_create,
    on_pre_update=_on_pre_update,
    on_pre_delete=_on_pre_delete,
)


__all__ = ["admin_users_router"]
