"""Principal — the resolved actor behind a single request.

A **value object**: built per-request by
:class:`primer.api.middleware.auth.AuthMiddleware` from the winning auth
path and stashed on ``scope.state.actor``. NEVER persisted (no
:class:`~primer.model.common.Identifiable` base, no storage backend).

Do not overload ``scope.state.principal`` (the bare username string kept
for backwards compatibility) — RBAC consumers read the richer
``scope.state.actor`` instead.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Principal(BaseModel):
    """Resolved identity + role behind a request.

    ``source`` records the auth channel that produced this principal:
    ``"local"`` for a password-login cookie session, the OIDC provider id
    (e.g. ``"okta"``) for an SSO-minted cookie session, or ``"internal"``
    for bearer-token / system (auth-disabled) callers. ``role`` is the
    RBAC role the request runs as, or ``None`` when unknown.
    """

    type: Literal["user", "trigger", "api_token", "system"] = Field(
        ..., description="Kind of actor behind the request.",
    )
    id: str = Field(..., description="Stable id of the actor.")
    display: str = Field(..., description="Human-readable label for logs / UI.")
    role: str | None = Field(
        default=None, description="RBAC role the request runs as, when known.",
    )
    source: str = Field(
        ...,
        description=(
            "Auth channel: \"local\" (password login), an OIDC provider id "
            "(SSO login), or \"internal\" (bearer token / system)."
        ),
    )


__all__ = ["Principal"]
