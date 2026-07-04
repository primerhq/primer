"""RBAC WS deps require_user_ws / require_role_ws — Spec §6.4.

Pure-function unit tests: a minimal websocket stand-in carries a
``state.user`` (a real ``User`` or ``None``); the role gate returns the
same user object or ``None``. No HTTP / event loop needed — the deps are
plain synchronous helpers mirroring ``require_auth_ws``.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.api.deps import require_role_ws, require_user_ws
from primer.model.user import User


def _ws(user):
    """Minimal websocket stand-in exposing ``.state.user`` (Starlette shape)."""
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


def _user(role: str) -> User:
    return User(
        id=f"user-{role}",
        username=role,
        password_hash="$argon2id$stub",
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        role=role,
    )


def test_require_user_ws_admin_and_user_pass():
    admin = _user("admin")
    user = _user("user")
    assert require_user_ws(_ws(admin)) is admin
    assert require_user_ws(_ws(user)) is user


def test_require_user_ws_restricted_and_unauth_none():
    assert require_user_ws(_ws(_user("restricted"))) is None
    assert require_user_ws(_ws(None)) is None


def test_require_role_ws_admin_min():
    admin = _user("admin")
    assert require_role_ws(_ws(admin), "admin") is admin
    assert require_role_ws(_ws(_user("user")), "admin") is None
    assert require_role_ws(_ws(_user("restricted")), "admin") is None


def test_require_role_ws_user_min():
    user = _user("user")
    admin = _user("admin")
    assert require_role_ws(_ws(user), "user") is user
    assert require_role_ws(_ws(admin), "user") is admin
    assert require_role_ws(_ws(_user("restricted")), "user") is None


def test_require_role_ws_unauth_none():
    assert require_role_ws(_ws(None), "user") is None
