"""Tests for primer.harness.hashes."""

from __future__ import annotations

import pytest

from primer.harness.hashes import (
    canonical_json,
    hash_bundle,
    hash_overrides,
    hash_rendered_payload,
    hash_schema,
    hash_template_source,
)


def test_canonical_json_sorts_keys():
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_canonical_json_compact_separators():
    assert canonical_json({"a": [1, 2]}) == '{"a":[1,2]}'


def test_hash_overrides_idempotent():
    h1 = hash_overrides({"a": 1, "b": 2})
    h2 = hash_overrides({"b": 2, "a": 1})
    assert h1 == h2
    assert len(h1) == 64


def test_hash_overrides_distinguishes_values():
    assert hash_overrides({"a": 1}) != hash_overrides({"a": 2})


def test_hash_schema_idempotent():
    a = {"type": "object", "properties": {"x": {"type": "string"}}}
    b = {"properties": {"x": {"type": "string"}}, "type": "object"}
    assert hash_schema(a) == hash_schema(b)


def test_hash_template_source_byte_sensitive():
    assert hash_template_source(b"abc") == hash_template_source(b"abc")
    assert hash_template_source(b"abc") != hash_template_source(b"abc ")


def test_hash_rendered_payload_idempotent():
    a = {"foo": [1, 2], "bar": "x"}
    b = {"bar": "x", "foo": [1, 2]}
    assert hash_rendered_payload(a) == hash_rendered_payload(b)


def test_hash_bundle_order_independent_to_walk():
    files = [
        ("a.yaml", b"contents-a"),
        ("b.yaml", b"contents-b"),
    ]
    h1 = hash_bundle(files)
    h2 = hash_bundle(list(reversed(files)))
    assert h1 == h2  # bundle hash sorts filenames internally


def test_hash_bundle_changes_with_content():
    files = [("a.yaml", b"original")]
    h1 = hash_bundle(files)
    h2 = hash_bundle([("a.yaml", b"modified")])
    assert h1 != h2
