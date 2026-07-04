"""Tests for the admin-bootstrap existing-install migration + break-glass.

``ensure_admin_exists`` runs on every boot (see
``primer.api._app_lifespan``). Fresh installs are unaffected — the
register endpoint already stamps the first account ``role="admin"``
(Task 2) — but an install that already had users before ``User.role``
existed would otherwise end up with zero admins after upgrading. This
promotes the oldest enabled, password-holding user to admin exactly
once to close that gap.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.auth.bootstrap_admin import ensure_admin_exists
from primer.model.storage import OffsetPage
from primer.model.user import User


def _make_user(
    *,
    id: str,
    username: str,
    created_at: datetime,
    role: str = "user",
    disabled: bool = False,
    password_hash: str | None = "$argon2id$fake$",
) -> User:
    return User(
        id=id,
        username=username,
        password_hash=password_hash,
        role=role,
        disabled=disabled,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_noop_when_no_users(fake_storage_provider):
    """Fresh install, zero users yet: no-op, no error."""
    await ensure_admin_exists(fake_storage_provider)

    storage = fake_storage_provider.get_storage(User)
    page = await storage.list(OffsetPage(offset=0, length=1))
    assert page.items == []


@pytest.mark.asyncio
async def test_noop_when_admin_already_exists(fake_storage_provider):
    storage = fake_storage_provider.get_storage(User)
    admin = _make_user(
        id="user-1",
        username="alice",
        role="admin",
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    other = _make_user(
        id="user-2",
        username="bob",
        role="user",
        created_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
    )
    await storage.create(admin)
    await storage.create(other)

    await ensure_admin_exists(fake_storage_provider)

    refreshed_other = await storage.get("user-2")
    assert refreshed_other.role == "user"  # untouched — an admin already exists


@pytest.mark.asyncio
async def test_promotes_oldest_eligible_user(fake_storage_provider):
    storage = fake_storage_provider.get_storage(User)
    newer = _make_user(
        id="user-2", username="bob", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    older = _make_user(
        id="user-1", username="alice", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    # Insert newer first to make sure "oldest" isn't accidentally "first
    # created in this call".
    await storage.create(newer)
    await storage.create(older)

    await ensure_admin_exists(fake_storage_provider)

    promoted = await storage.get("user-1")
    untouched = await storage.get("user-2")
    assert promoted.role == "admin"
    assert untouched.role == "user"


@pytest.mark.asyncio
async def test_skips_disabled_and_passwordless_users(fake_storage_provider):
    storage = fake_storage_provider.get_storage(User)
    disabled_oldest = _make_user(
        id="user-1",
        username="disabled-user",
        disabled=True,
        created_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
    )
    passwordless = _make_user(
        id="user-2",
        username="invited-user",
        password_hash=None,
        created_at=datetime(2019, 6, 1, tzinfo=timezone.utc),
    )
    eligible = _make_user(
        id="user-3",
        username="eligible-user",
        created_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
    )
    for u in (disabled_oldest, passwordless, eligible):
        await storage.create(u)

    await ensure_admin_exists(fake_storage_provider)

    assert (await storage.get("user-1")).role == "user"
    assert (await storage.get("user-2")).role == "user"
    assert (await storage.get("user-3")).role == "admin"


@pytest.mark.asyncio
async def test_promotes_eligible_user_when_only_admin_is_unusable(fake_storage_provider):
    """A role='admin' user who is disabled (or password-less) cannot
    actually log in as admin. The break-glass backfill must not treat
    such an unusable admin as "an admin already exists" — otherwise an
    eligible, usable user is left stranded at role='user' with no way
    to reach admin-gated functionality (the exact lockout this function
    exists to prevent)."""
    storage = fake_storage_provider.get_storage(User)
    unusable_admin = _make_user(
        id="user-1",
        username="locked-admin",
        role="admin",
        disabled=True,
        created_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
    )
    eligible_user = _make_user(
        id="user-2",
        username="eligible",
        role="user",
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    await storage.create(unusable_admin)
    await storage.create(eligible_user)

    await ensure_admin_exists(fake_storage_provider)

    assert (await storage.get("user-2")).role == "admin"
    # the unusable admin is left as-is; it's still disabled either way
    assert (await storage.get("user-1")).role == "admin"


@pytest.mark.asyncio
async def test_noop_when_no_eligible_user(fake_storage_provider):
    """Every existing user is disabled/passwordless — nothing eligible
    to promote, and the function must not raise."""
    storage = fake_storage_provider.get_storage(User)
    disabled = _make_user(
        id="user-1",
        username="disabled-user",
        disabled=True,
        created_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
    )
    await storage.create(disabled)

    await ensure_admin_exists(fake_storage_provider)

    assert (await storage.get("user-1")).role == "user"
