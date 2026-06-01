"""ApiToken model — programmatic-access credential.

See ``docs/superpowers/specs/2026-06-02-api-tokens-bearer-auth-design.md`` §3.

A row per minted token. The plaintext is NEVER stored — only
``token_hash`` (sha256 hex of the plaintext bytes) sits in the row.
Plaintext is returned to the operator once at creation; loss of the
plaintext requires minting a new token.

Single-user platform today; ``user_id`` is always the lone user's id,
but the schema is multi-user ready.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from primer.model.common import Identifiable


class ApiToken(Identifiable):
    """Long-lived bearer credential for programmatic access.

    Plaintext token NEVER stored; only sha256(plaintext). Plaintext is
    returned to the client ONCE at creation and can't be retrieved again —
    operator loses it → must mint a new one.
    """

    user_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=128)
    token_hash: str = Field(..., min_length=64, max_length=64)  # sha256 hex
    prefix: str = Field(..., min_length=8, max_length=8)
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must be non-empty")
        return v

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: list[str]) -> list[str]:
        """Normalise: lowercase, dedup, drop empty."""
        out: list[str] = []
        seen: set[str] = set()
        for s in v:
            n = s.strip().lower()
            if not n or n in seen:
                continue
            seen.add(n)
            out.append(n)
        return out


# Known scopes (informational; validation is open — adding new scopes
# doesn't need a code change).
SCOPE_MCP = "mcp"
KNOWN_SCOPES = frozenset({SCOPE_MCP})


__all__ = ["ApiToken", "SCOPE_MCP", "KNOWN_SCOPES"]
