"""Point-to-templatize logic — Spec B §4."""

from __future__ import annotations

import pytest

from primer.harness.templatize import (
    OverrideSchemaCollisionError,
    apply_override_mappings,
    compose_overrides_schema_from_mappings,
    infer_schema_fragment,
)
from primer.model.harness import OverrideMapping


def test_infer_string():
    s = infer_schema_fragment("openai")
    assert s == {"type": "string", "default": "openai"}


def test_infer_int():
    assert infer_schema_fragment(3)["type"] == "integer"


def test_infer_float():
    assert infer_schema_fragment(0.2)["type"] == "number"


def test_infer_list_of_strings():
    s = infer_schema_fragment(["a", "b"])
    assert s["type"] == "array"
    assert s["items"]["type"] == "string"


def test_apply_replaces_at_pointer():
    entity = {"model": {"provider_id": "openai", "model_name": "gpt-4"}, "temperature": 0.2}
    mappings = [
        OverrideMapping(field_path="/model/provider_id", override_path="llm.provider_id"),
    ]
    out = apply_override_mappings(entity, mappings)
    assert out["model"]["provider_id"] == "{{ overrides.llm.provider_id }}"
    assert out["model"]["model_name"] == "gpt-4"


def test_compose_schema_nested_path():
    mappings = [
        OverrideMapping(
            field_path="/model/provider_id",
            override_path="llm.provider_id",
            widget="llm-provider-picker",
        ),
        OverrideMapping(field_path="/model/model_name", override_path="llm.model_name"),
    ]
    values = {"/model/provider_id": "openai", "/model/model_name": "gpt-4"}
    schema = compose_overrides_schema_from_mappings(mappings, values)
    assert schema["properties"]["llm"]["properties"]["provider_id"]["default"] == "openai"
    assert (
        schema["properties"]["llm"]["properties"]["provider_id"]["x-primer-widget"]
        == "llm-provider-picker"
    )


def test_collision_raises():
    mappings = [
        OverrideMapping(field_path="/x", override_path="a"),
        OverrideMapping(field_path="/y", override_path="a"),  # same override_path, different value
    ]
    values = {"/x": "one", "/y": 2}
    with pytest.raises(OverrideSchemaCollisionError):
        compose_overrides_schema_from_mappings(mappings, values)


def test_invalid_field_path_raises():
    entity = {"a": 1}
    mappings = [OverrideMapping(field_path="/nope/deep", override_path="x")]
    with pytest.raises(KeyError):
        apply_override_mappings(entity, mappings)


def test_infer_bool_before_int():
    """Python gotcha: bool subclasses int. Ensure True/False infer boolean."""
    s_true = infer_schema_fragment(True)
    assert s_true == {"type": "boolean", "default": True}
    s_false = infer_schema_fragment(False)
    assert s_false == {"type": "boolean", "default": False}


def test_compose_widget_propagated():
    mappings = [
        OverrideMapping(
            field_path="/provider_id",
            override_path="ssp.provider_id",
            widget="ssp-picker",
        ),
    ]
    values = {"/provider_id": "weaviate"}
    schema = compose_overrides_schema_from_mappings(mappings, values)
    leaf = schema["properties"]["ssp"]["properties"]["provider_id"]
    assert leaf["x-primer-widget"] == "ssp-picker"
    assert leaf["default"] == "weaviate"
    assert leaf["type"] == "string"


def test_schema_override_shallow_merges():
    """schema_override keys override the inferred ones (shallow merge)."""
    mappings = [
        OverrideMapping(
            field_path="/temperature",
            override_path="llm.temperature",
            schema_override={"type": "number", "minimum": 0, "maximum": 2, "default": 0.7},
        ),
    ]
    values = {"/temperature": 0.2}
    schema = compose_overrides_schema_from_mappings(mappings, values)
    leaf = schema["properties"]["llm"]["properties"]["temperature"]
    # schema_override's default wins over the inferred 0.2
    assert leaf["default"] == 0.7
    # extra keys from schema_override survive
    assert leaf["minimum"] == 0
    assert leaf["maximum"] == 2
    # type remains (both said number)
    assert leaf["type"] == "number"
