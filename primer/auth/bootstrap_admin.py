"""Admin bootstrap — existing-install migration + break-glass.

Layer 1 RBAC introduces ``User.role`` (Task 2), defaulting existing
rows to ``"user"``. A fresh install is unaffected — ``POST
/v1/auth/register`` already stamps the first account ``role="admin"``
— but an install that already had users *before* ``role`` existed
would otherwise come up with **zero** admin accounts after upgrading,
locking the operator out of any admin-gated functionality with no way
back in short of manual DB surgery.

:func:`ensure_admin_exists` closes that gap: called once per boot (see
``primer.api._app_lifespan``), it promotes the oldest enabled,
password-holding user to admin if no admin currently exists. It is
idempotent — once an admin exists, every subsequent call is a no-op.
"""

from __future__ import annotations

import logging

from primer.model.storage import OffsetPage
from primer.model.user import User


logger = logging.getLogger(__name__)

# Self-hosted, small-scale operator tool — a single page comfortably
# covers every realistic install's user count (OffsetPage.length caps
# at 200; see primer.model.storage.OffsetPage).
_PAGE_SIZE = 200


async def ensure_admin_exists(storage_provider) -> None:
    """Promote the oldest enabled, password-holding user to admin if no
    admin currently exists.

    No-op when: there are no users yet (fresh install); an admin
    already exists; or every existing user is disabled or
    password-less (nothing eligible to promote).
    """
    storage = storage_provider.get_storage(User)

    users: list[User] = []
    offset = 0
    while True:
        page = await storage.list(OffsetPage(offset=offset, length=_PAGE_SIZE))
        users.extend(page.items)
        if len(page.items) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    # Only accounts that can actually log in locally count as "usable" —
    # a disabled or password-less role="admin" row must not be treated as
    # satisfying "an admin already exists", or the eligible user below
    # would be left permanently locked out.
    eligible = [u for u in users if not u.disabled and u.password_hash]
    if any(u.role == "admin" for u in eligible):
        return  # a usable admin already exists — nothing to do

    if not eligible:
        return  # no eligible user to promote

    candidate = min(eligible, key=lambda u: u.created_at)
    promoted = candidate.model_copy(update={"role": "admin"})
    await storage.update(promoted)
    logger.warning(
        "bootstrap: no admin user found; promoted %s (id=%s) to admin",
        promoted.username,
        promoted.id,
    )


__all__ = ["ensure_admin_exists"]
