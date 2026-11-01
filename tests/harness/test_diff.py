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


# GX-5: a harness with dependencies renders the parent's and every sub-harness's templates into one list, so two
# entries may share (kind, template_name) and differ only by resolved id (``dep__assistant``, ``acme__assistant``).


def test_entries_sharing_a_template_name_are_diffed_by_resolved_id():
    old = [_entry("agent", "assistant", "d1", "dep__assistant"), _entry("agent", "assistant", "p1", "acme__assistant")]
    new = [_entry("agent", "assistant", "d2", "dep__assistant"), _entry("agent", "assistant", "p1", "acme__assistant")]
    d = diff_renderings(old, new)
    assert [(o.resolved_id, n.rendered_hash) for o, n in d.updates] == [("dep__assistant", "d2")]
    assert [e.resolved_id for e in d.noops] == ["acme__assistant"]
    assert d.creates == [] and d.deletes == []


def test_a_dropped_entry_sharing_a_template_name_is_deleted():
    old = [_entry("agent", "assistant", "d1", "dep__assistant"), _entry("agent", "assistant", "p1", "acme__assistant")]
    new = [_entry("agent", "assistant", "p1", "acme__assistant")]
    d = diff_renderings(old, new)
    assert [e.resolved_id for e in d.deletes] == ["dep__assistant"]
    assert [e.resolved_id for e in d.noops] == ["acme__assistant"]


def test_an_old_format_snapshot_diffs_against_a_new_rendering():
    """A snapshot stored before ``source_dependency`` / ``source_entity_id`` existed still carries ``resolved_id`` (a
    required field since the model was introduced), so it diffs by the same key as a fresh rendering."""
    from primer.model.harness import HarnessRendering

    def row(kind, name, rid, rendered_hash):
        return {"kind": kind, "template_name": name, "resolved_id": rid, "template_source_hash": "ts",
                "rendered_hash": rendered_hash, "rendered_payload": {"x": rendered_hash}}

    stored = HarnessRendering.model_validate({
        "id": "h", "harness_id": "h", "bundle_hash": "b0", "overrides_hash": "o", "schema_hash": None,
        "rendered_at": "2026-05-28T00:00:00Z",
        "entries": [row("agent", "a", "s__a", "h1"), row("agent", "b", "s__b", "h1"), row("graph", "a", "s__a", "h1")],
    })
    new = [_entry("agent", "a", "h1"), _entry("agent", "b", "h2"), _entry("collection", "c", "h1")]
    d = diff_renderings(stored.entries, new)
    assert [e.resolved_id for e in d.noops] == ["s__a"]
    assert [(o.resolved_id, n.rendered_hash) for o, n in d.updates] == [("s__b", "h2")]
    assert [(e.kind, e.resolved_id) for e in d.creates] == [("collection", "s__c")]
    assert [(e.kind, e.resolved_id) for e in d.deletes] == [("graph", "s__a")]
