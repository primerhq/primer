import json

import pytest

from primectl.manifest import parse_manifests, dump_envelope, ManifestError


def test_parse_single_doc():
    text = "kind: agent\nspec:\n  id: a1\n  model: gpt\n"
    docs = parse_manifests(text)
    assert docs == [("agent", {"id": "a1", "model": "gpt"})]


def test_parse_multi_doc():
    text = (
        "kind: agent\nspec:\n  id: a1\n---\n"
        "kind: graph\nspec:\n  id: g1\n"
    )
    docs = parse_manifests(text)
    assert [d[0] for d in docs] == ["agent", "graph"]
    assert docs[1][1]["id"] == "g1"


def test_missing_kind_raises():
    with pytest.raises(ManifestError):
        parse_manifests("spec:\n  id: a1\n")


def test_missing_spec_raises():
    with pytest.raises(ManifestError):
        parse_manifests("kind: agent\n")


def test_dump_envelope_yaml_roundtrips():
    out = dump_envelope("agent", {"id": "a1", "model": "gpt"}, fmt="yaml")
    docs = parse_manifests(out)
    assert docs == [("agent", {"id": "a1", "model": "gpt"})]


def test_dump_envelope_json():
    out = dump_envelope("agent", {"id": "a1"}, fmt="json")
    obj = json.loads(out)
    assert obj == {"kind": "agent", "spec": {"id": "a1"}}
