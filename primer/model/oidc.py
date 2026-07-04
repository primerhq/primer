"""OIDC SSO models (Layer 2)."""
from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field, SecretStr

from primer.model.common import Identifiable


class OidcProvider(Identifiable):
    _id_prefix: ClassVar[str] = "oidc-provider"
    name: str = Field(..., min_length=1, max_length=128)
    discovery_url: str = Field(..., description="OIDC issuer .well-known/openid-configuration URL")
    client_id: str = Field(..., min_length=1)
    client_secret: SecretStr | None = Field(default=None)
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])
    enabled: bool = Field(default=True)


class UserIdentity(Identifiable):
    _id_prefix: ClassVar[str] = "user-identity"
    user_id: str = Field(..., min_length=1)
    provider_id: str = Field(..., min_length=1)
    subject: str = Field(..., min_length=1, description="OIDC 'sub' claim")
    email: str | None = Field(default=None, description="display only; only set when email_verified")
    created_at: datetime = Field(...)
