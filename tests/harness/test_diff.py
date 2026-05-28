"""Tests for primer.harness.diff."""

from __future__ import annotations

from primer.harness.diff import DiffOp, diff_renderings
from primer.model.harness import RenderedEntry


def _entry(kind, name, rendered_hash, resolved_id=None):
    return RenderedEntry(
        kind=kind, template_name=name,
        resolved_id=resolved_id or f"s__{name}",
        template_source_hash="ts", rendered_hash=rendered_hash,
        rendered_payload={"x": rendered_hash},
    )


def test_noop_when_identical():
    old = [_entry("agent", "a", "h1")]
    new = [_entry("agent", "a", "h1")]
    d = diff_renderings(old, new)
    assert d.creates == []
    assert d.updates == []
    assert d.deletes == []
    assert len(d.noops) == 1


def test_create_when_only_new():
    new = [_entry("agent", "a", "h1")]
    d = diff_renderings([], new)
    assert len(d.creates) == 1
    assert d.creates[0].template_name == "a"


def test_delete_when_only_old():
    old = [_entry("agent", "a", "h1")]
    d = diff_renderings(old, [])
    assert len(d.deletes) == 1


def test_update_when_hash_changes():
    old = [_entry("agent", "a", "h1")]
    new = [_entry("agent", "a", "h2")]
    d = diff_renderings(old, new)
    assert len(d.updates) == 1
    assert d.updates[0][1].rendered_hash == "h2"


def test_kind_partitions_namespace():
    """An 'agent' named 'x' is independent of a 'graph' named 'x'."""
    old = [_entry("agent", "x", "h1")]
    new = [_entry("graph", "x", "h1")]
    d = diff_renderings(old, new)
    assert len(d.deletes) == 1
    assert len(d.creates) == 1
