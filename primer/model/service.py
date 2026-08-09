"""A published, versioned web app served at ``/svc/{name}/``.

A ``Service`` is a named routing row: the slug, the pointer to the active
version, and the operator-controlled exposure setting. A
``ServiceVersion`` is one immutable published snapshot: the manifest, the
bundle files (as :class:`~primer.model.artifact.Artifact` references),
and the function specs derived at publish time so the serving and
gateway paths never re-parse source.

Design spec: ``docs/superpowers/specs/2026-08-08-services-design.md``
sections 4-5. The exposure setting (``viewer_auth``) lives HERE and not
in the manifest deliberately: the manifest travels with the code, and a
publish must never silently flip a service public.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from primer.model.common import Describeable, Identifiable

SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
"""Slug rule: 2-63 chars, lowercase alphanumerics and dashes, no leading dash."""

RESERVED_SERVICE_NAMES = frozenset({"_client"})
"""Path segments under ``/svc/`` that are routes, not service names."""


class ServiceViewerAuth(str, Enum):
    """Who may view (and, with it, call) a service.

    ``CONSOLE`` requires the console's own authentication on every
    ``/svc/{name}/*`` request. ``NONE`` serves anonymously; the manifest
    tool allowlist still applies to gateway calls in both modes.
    """

    CONSOLE = "console"
    NONE = "none"


class ServiceToolGrant(BaseModel):
    """One allowlist entry: a toolset, optionally narrowed to named tools."""

    toolset_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the granted Toolset.",
    )
    tool_names: list[str] | None = Field(
        default=None,
        description="Specific tools granted; None grants every tool in the toolset.",
    )


class ServiceManifest(BaseModel):
    """The bundle's ``service.yaml``: code-coupled configuration.

    Unknown keys are rejected so a typo fails the publish rather than
    silently doing nothing.
    """

    model_config = ConfigDict(extra="forbid")

    entry: str = Field(
        default="index.html",
        description="Bundle path served for '/' and as the SPA fallback.",
    )
    functions: list[str] = Field(
        default_factory=list,
        description=(
            "Python files whose @primer_tool functions become gateway "
            "functions. Defaults to ['functions.py'] when that file "
            "exists in the bundle."
        ),
    )
    tools: list[ServiceToolGrant] = Field(
        default_factory=list,
        description=(
            "Gateway tool allowlist, enforced in BOTH viewer_auth modes."
        ),
    )


class Service(Describeable):
    """One registered service: the routing row behind ``/svc/{name}/``."""

    _id_prefix: ClassVar[str] = "service"

    name: str = Field(
        ...,
        description=(
            "URL-safe slug and the /svc route key. Unique across "
            "services; renaming a published service is rejected because "
            "the name is the public URL."
        ),
    )
    active_version_id: str | None = Field(
        default=None,
        description=(
            "The ServiceVersion currently served; None means created "
            "but not yet published."
        ),
    )
    viewer_auth: ServiceViewerAuth = Field(
        default=ServiceViewerAuth.CONSOLE,
        description="Operator-controlled exposure; see ServiceViewerAuth.",
    )
    harness_id: str | None = Field(
        default=None,
        description=(
            "Set when this row is managed by a harness; direct CRUD and "
            "publish are then rejected."
        ),
    )

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SERVICE_NAME_RE.match(v) or v in RESERVED_SERVICE_NAMES:
            raise ValueError(
                "service name must match [a-z0-9][a-z0-9-]{1,62} and not "
                "be a reserved segment"
            )
        return v


class ServiceFunctionSpec(BaseModel):
    """One gateway function, derived at publish time from bundle source."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., description="Function name; the _gateway path segment.")
    schema_: dict[str, Any] = Field(
        ...,
        alias="schema",
        description="JSON schema for the arguments, derived from the signature.",
    )
    timeout_seconds: float = Field(
        ...,
        description="Per-call wall-clock timeout enforced by the runner.",
    )
    source_file: str = Field(
        ...,
        description="Bundle path of the module this function was registered from.",
    )


class ServiceVersion(Identifiable):
    """One immutable published snapshot of a service's bundle."""

    _id_prefix: ClassVar[str] = "service-version"

    service_id: str = Field(..., description="Owning Service id.")
    version: int = Field(
        ...,
        description="Monotonic per-service version number, starting at 1.",
    )
    manifest: ServiceManifest = Field(
        ...,
        description="The parsed service.yaml this version was published with.",
    )
    files: dict[str, str] = Field(
        ...,
        description="Bundle path -> Artifact id for every file in the snapshot.",
    )
    functions: list[ServiceFunctionSpec] = Field(
        default_factory=list,
        description="Gateway functions registered at publish time.",
    )


__all__ = [
    "RESERVED_SERVICE_NAMES",
    "SERVICE_NAME_RE",
    "Service",
    "ServiceFunctionSpec",
    "ServiceManifest",
    "ServiceToolGrant",
    "ServiceVersion",
    "ServiceViewerAuth",
]
