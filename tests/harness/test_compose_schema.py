"""Composite override schema build + per-level slicing — Spec A §6, §8."""

from __future__ import annotations

from primer.harness.template import compose_overrides_schema, slice_overrides_for_dep


def test_compose_flat_no_deps():
    parent = {"type": "object", "properties": {"x": {"type": "string"}}}
    out = compose_overrides_schema(parent_schema=parent, sub_schemas=[])
    assert out == parent


def test_compose_mounts_deps_under_dependencies():
    parent = {"type": "object", "properties": {"x": {"type": "string"}}}
    sub_a = {"type": "object", "properties": {"a": {"type": "string"}}}
    sub_b = {"type": "object", "properties": {"b": {"type": "integer"}}}
    out = compose_overrides_schema(
        parent_schema=parent,
        sub_schemas=[("docs", sub_a), ("tools", sub_b)],
    )
    assert out["properties"]["dependencies"]["properties"]["docs"] == sub_a
    assert out["properties"]["dependencies"]["properties"]["tools"] == sub_b
    assert out["properties"]["x"] == {"type": "string"}


def test_slice_extracts_dep_subtree():
    overrides = {"llm": {"x": "y"}, "dependencies": {"docs": {"a": 1}}}
    assert slice_overrides_for_dep(overrides, "docs") == {"a": 1}


def test_slice_missing_returns_empty():
    overrides = {"llm": {"x": "y"}}
    assert slice_overrides_for_dep(overrides, "docs") == {}
