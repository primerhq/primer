"""User model — Layer 1 identity/RBAC fields (Task 2).

Extends the single-user v1 model with the fields multi-user + RBAC
needs: ``email``, ``role``, ``disabled``, ``must_change_password``.
``password_hash`` becomes optional so an account can be provisioned
without a password (e.g. invited, or migrated) — see
``primer.auth.passwords.verify_password`` which already treats a
``None``/empty hash as "can never authenticate".
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.model.user import User


def _base_kwargs(**overrides):
    kwargs = dict(
        id="user-1",
        username="alice",
        password_hash="$argon2id$fake$",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    kwargs.update(overrides)
    return kwargs


def test_defaults_role_user_not_disabled_no_email():
    u = User(**_base_kwargs())
    assert u.role == "user"
    assert u.disabled is False
    assert u.must_change_password is False
    assert u.email is None


def test_password_hash_accepts_none():
    """An account provisioned without a password (e.g. invited) is valid."""
    u = User(**_base_kwargs(password_hash=None))
    assert u.password_hash is None


def test_role_and_disabled_and_must_change_password_settable():
    u = User(
        **_base_kwargs(
            role="admin",
            disabled=True,
            must_change_password=True,
            email="alice@example.com",
        )
    )
    assert u.role == "admin"
    assert u.disabled is True
    assert u.must_change_password is True
    assert u.email == "alice@example.com"
