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

from pydantic import BaseModel, ConfigDict, Field


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


class PrincipalRef(BaseModel):
    """Persisted PROJECTION of a :class:`Principal` (Layer 3, §8.2).

    :class:`Principal` is a per-request value object that is NEVER
    persisted. When a run must record *who initiated it* on a durable
    row (``WorkspaceSession.initiated_by`` / ``Chat.initiated_by``) we
    store this small frozen projection instead, then re-hydrate it into
    ``ctx.identity`` on the async/worker side so the originating identity
    survives the boundary (a trigger-fired run stays a trigger). Carries
    no secrets — exactly the five fields exposed as ``ctx.identity``.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["user", "trigger", "api_token", "system"] = Field(
        ..., description="Kind of actor that initiated the run.",
    )
    id: str = Field(..., description="Stable id of the initiating actor.")
    display: str = Field(..., description="Human-readable label for logs / UI.")
    role: str | None = Field(
        default=None, description="RBAC role the run was initiated under, when known.",
    )
    source: str = Field(
        ..., description='Auth channel: "local" | "<oidc-provider-id>" | "internal".',
    )

    @classmethod
    def from_principal(cls, p: "Principal") -> "PrincipalRef":
        """Project a live :class:`Principal` into its persisted form."""
        return cls(
            type=p.type, id=p.id, display=p.display, role=p.role, source=p.source,
        )

    @classmethod
    def system(cls) -> "PrincipalRef":
        """The reserved system principal — the fallback for missing/historical
        ``initiated_by`` (§8.2, §13) and for auth-disabled / internal runs."""
        return cls(
            type="system", id="system", display="system", role=None,
            source="internal",
        )


__all__ = ["Principal", "PrincipalRef"]
