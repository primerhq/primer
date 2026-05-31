"""Subharness dependency model fields — Spec A §5."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.harness import (
    DependencyRef,
    Harness,
    HarnessStatus,
    RenderedEntry,
    ResolvedDependency,
)
from datetime import datetime, timezone


def test_dependency_ref_accepts_valid_name():
    d = DependencyRef(name="docs", git_url="https://github.com/x/y", ref="main")
    assert d.name == "docs"


def test_dependency_ref_rejects_invalid_name():
    with pytest.raises(ValidationError):
        DependencyRef(name="Docs!", git_url="https://x", ref="main")


def test_resolved_dependency_round_trips():
    r = ResolvedDependency(
        name="docs", slug="docs-base", git_url="https://x",
        ref="main", subpath=None, resolved_commit="a"*40,
        bundle_hash="b"*64, depth=0, parent_name=None,
    )
    assert r.depth == 0


def test_harness_default_dependencies_resolved_is_empty():
    h = Harness(
        id="hn-x", slug="xy", name="X", git_url="https://x",
        created_at=datetime.now(timezone.utc),
    )
    assert h.dependencies_resolved == []


def test_rendered_entry_default_source_dependency_is_none():
    e = RenderedEntry(
        kind="agent", template_name="bot", resolved_id="x__bot",
        template_source_hash="s"*64, rendered_hash="r"*64,
        rendered_payload={},
    )
    assert e.source_dependency is None
