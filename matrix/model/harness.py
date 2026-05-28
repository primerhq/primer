"""Harness models — see docs/superpowers/specs/2026-05-27-harness-design.md §5."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from matrix.model.common import Identifiable


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


class HarnessRendering(Identifiable):
    harness_id: str
    bundle_hash: str
    overrides_hash: str
    schema_hash: str | None
    entries: list[RenderedEntry]
    rendered_at: datetime


__all__ = [
    "Harness",
    "HarnessOperation",
    "HarnessRendering",
    "HarnessStatus",
    "RenderedEntry",
]
