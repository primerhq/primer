"""Harness models — see docs/superpowers/specs/2026-05-27-harness-design.md §5."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from primer.model.common import Identifiable


class HarnessStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    INSTALLED = "installed"
    OUTDATED = "outdated"
    ERROR = "error"


class HarnessOperation(str, Enum):
    FETCH = "fetch"
    INSTALL = "install"
    SYNC = "sync"
    UNINSTALL = "uninstall"


_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_DEP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class DependencyRef(BaseModel):
    """Declared subharness dependency from a parent harness.yaml."""

    name: str = Field(..., min_length=1, max_length=64)
    git_url: str = Field(..., min_length=1)
    ref: str = Field(default="main", min_length=1)
    subpath: str | None = None
    git_token: SecretStr | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _DEP_NAME_RE.match(v):
            raise ValueError(
                "dependency name must match [a-z][a-z0-9-]{0,63}",
            )
        return v


class ResolvedDependency(BaseModel):
    """A dependency node resolved by the transitive walk."""

    name: str
    slug: str
    git_url: str
    ref: str
    subpath: str | None = None
    resolved_commit: str
    bundle_hash: str
    depth: int = Field(..., ge=0)
    parent_name: str | None = None


class Harness(Identifiable):
    slug: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    git_url: str = Field(..., min_length=1)
    git_token: SecretStr | None = None
    subpath: str | None = None
    ref: str = Field(default="main", min_length=1)
    overrides: dict[str, Any] = Field(default_factory=dict)
    overrides_schema: dict[str, Any] | None = None
    overrides_hash: str | None = None
    schema_hash: str | None = None
    resolved_commit: str | None = None
    available_commit: str | None = None
    bundle_hash: str | None = None
    available_bundle_hash: str | None = None
    status: HarnessStatus = HarnessStatus.DRAFT
    commits_ahead: bool = False
    overrides_dirty: bool = False
    schema_missing_input: bool = False
    pending_operation: HarnessOperation | None = None
    last_operation_at: datetime | None = None
    last_operation_error: str | None = None
    dependencies_resolved: list[ResolvedDependency] = Field(default_factory=list)
    created_at: datetime

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must match [a-z][a-z0-9-]{1,63}",
            )
        if "__" in v:
            raise ValueError("slug may not contain '__'")
        return v


class RenderedEntry(BaseModel):
    kind: Literal["agent", "graph", "collection", "document", "toolset"]
    template_name: str = Field(..., min_length=1, max_length=64)
    resolved_id: str
    template_source_hash: str
    rendered_hash: str
    rendered_payload: dict[str, Any]
    source_dependency: str | None = None


class HarnessRendering(Identifiable):
    harness_id: str
    bundle_hash: str
    overrides_hash: str
    schema_hash: str | None
    entries: list[RenderedEntry]
    rendered_at: datetime


__all__ = [
    "DependencyRef",
    "Harness",
    "HarnessOperation",
    "HarnessRendering",
    "HarnessStatus",
    "RenderedEntry",
    "ResolvedDependency",
]
