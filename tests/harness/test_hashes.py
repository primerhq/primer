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
    contents = {"a.yaml": b"contents-a", "b.yaml": b"contents-b"}
    paths = ["a.yaml", "b.yaml"]
    h1 = hash_bundle(paths, contents.__getitem__)
    h2 = hash_bundle(list(reversed(paths)), contents.__getitem__)
    assert h1 == h2  # bundle hash sorts filenames internally


def test_hash_bundle_changes_with_content():
    h1 = hash_bundle(["a.yaml"], {"a.yaml": b"original"}.__getitem__)
    h2 = hash_bundle(["a.yaml"], {"a.yaml": b"modified"}.__getitem__)
    assert h1 != h2


def test_hash_bundle_streams_one_file_at_a_time(tmp_path):
    """Pre-fix red: hash_bundle materialised every file of the checkout in
    one ``[(path, bytes), ...]`` list before digesting. It now takes paths
    plus a reader and digests file by file, so only one file's content is
    held at a time. The digest framing (sorted by path, ``path\\x00content\\x00``)
    is unchanged, so stored bundle hashes - which drive the sync fast path -
    are identical to the pre-fix digest computed inline below."""
    import hashlib

    contents = {
        "harness.yaml": b"kind: Harness",
        "templates/one.yaml": b"spec: one",
        "templates/two.yaml": b"spec: two",
    }
    for rel, data in contents.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    # Reference digest: the pre-fix framing, computed inline.
    h = hashlib.sha256()
    for rel in sorted(contents):
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(contents[rel])
        h.update(b"\x00")
    reference = h.hexdigest()

    reads: list[str] = []

    def read(name: str) -> bytes:
        reads.append(name)
        return (tmp_path / name).read_bytes()

    assert hash_bundle(list(contents), read) == reference
    assert reads == sorted(contents), "one read per file, in sorted order"
