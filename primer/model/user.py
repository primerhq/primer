"""User model - the operator accounts that can log in to the primer console.

Multi-user is in place (access-control Phase 2 and on): a row is one
person; ``role`` (``admin`` / ``user`` / ``restricted``) is their rank,
``disabled`` locks the account out, ``must_change_password`` forces a
reset before continued use, and ``session_epoch`` is the generation
number every session cookie carries (a password change, an admin reset
or "sign out everywhere" bumps it and ends all of the account's
sessions). ``POST /v1/auth/register`` stays a FIRST-BOOT door: it is
valid only when no user exists yet and creates the install's first
admin; every other account is provisioned by an admin through
``/v1/admin/users``.

The ``password_hash`` field stores the full argon2 PHC-format string
(``$argon2id$...``) including algorithm parameters and salt; the
verifier uses the embedded parameters so we can rotate hash params
without invalidating existing hashes. ``None`` marks an account
provisioned without a password; it cannot authenticate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from primer.model.common import Identifiable


class User(Identifiable):
    """Operator account.

    ``username`` is the login identifier; it's stored lowercase-
    normalised by the auth router. ``password_hash`` is the full
    argon2id PHC string."""

    username: str = Field(
        ...,
        description="Lowercase-normalised login identifier. Unique across "
        "the table through the auth/admin checks and the storage providers' "
        "unique username index.",
        min_length=1,
        max_length=64,
    )
    password_hash: str | None = Field(
        default=None,
        description="Full argon2id PHC string (``$argon2id$...``). Includes "
        "salt + cost parameters; verification uses the embedded parameters. "
        "``None`` for accounts provisioned without a password (e.g. invited "
        "but not yet activated); ``verify_password`` treats a ``None``/empty "
        "hash as never-matching, so such an account can't authenticate.",
    )
    created_at: datetime = Field(..., description="When the account was created.")
    last_login_at: datetime | None = Field(
        default=None,
        description="When the user most recently completed a successful login.",
    )
    email: str | None = Field(
        default=None,
        description="Optional contact email. Not used for login (username "
        "is the identifier) — reserved for notifications / password-reset "
        "flows in a later layer.",
    )
    role: Literal["admin", "user", "restricted"] = Field(
        default="user",
        description="Access-control role. 'admin' can manage other users "
        "and RBAC-gated resources; 'user' is a standard operator account. "
        "Defaults to 'user' so existing constructors keep working; call "
        "sites that provision the admin account pass role='admin' explicitly.",
    )
    default_workspace_id: str | None = Field(
        default=None,
        description="The user's private default workspace (access-control spec 6.7): created at their first sign-in as a "
        "full user and never deletable. None for a user who has not yet signed in as a full user (a restricted user has none).",
    )
    disabled: bool = Field(
        default=False,
        description="If True, the account is locked out: auth middleware "
        "and login must reject it even given a valid password or session.",
    )
    must_change_password: bool = Field(
        default=False,
        description="If True, the user must set a new password before "
        "continuing to use the app (e.g. after an admin-initiated reset).",
    )
    session_epoch: int = Field(
        default=0,
        ge=0,
        description="Session generation (SEC-05). Every signed session cookie "
        "carries the epoch it was minted under, and the auth middleware treats "
        "a cookie from any other epoch as no session. A password change, an "
        "admin password reset and 'sign out everywhere' increment it, which "
        "ends every session of the account at once. Rows written before the "
        "field existed load as 0, matching legacy cookies without an epoch.",
    )
    welcome_acked_roles: frozenset[Literal["admin", "user"]] = Field(
        default_factory=frozenset,
        description="Roles whose welcome this person has seen. Older rows default to an empty set. "
        "Independent of session creation, setup completion and the authentication epoch.",
    )
