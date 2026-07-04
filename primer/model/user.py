"""User model — single-user authentication for v1.

A row in this table represents the operator account that can log in to
the primer console. Single-user enforcement is in the auth router:
``POST /v1/auth/register`` is only valid when no user exists. v2 will
introduce multi-user with invitations.

The ``password_hash`` field stores the full argon2 PHC-format string
(``$argon2id$...``) including algorithm parameters and salt; the
verifier uses the embedded parameters so we can rotate hash params
without invalidating existing hashes.
"""

from __future__ import annotations

from datetime import datetime

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
        "the table (enforced by application code; the storage layer's "
        "predicate ABC does not currently express uniqueness constraints).",
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
    role: str = Field(
        default="user",
        description="Access-control role. 'admin' can manage other users "
        "and RBAC-gated resources; 'user' is a standard operator account. "
        "Defaults to 'user' so existing constructors keep working; call "
        "sites that provision the admin account pass role='admin' explicitly.",
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
