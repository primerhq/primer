import json

import yaml

from primectl.output import render, derive_columns


AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "description": {"type": "string"},
        "model": {"type": "string"},
        "tools": {"type": "array"},
        "system_prompt": {"type": "array"},
    },
}


def test_derive_columns_prefers_id_and_scalars():
    cols = derive_columns(AGENT_SCHEMA, wide=False)
    assert cols[0] == "id"
    assert "model" in cols
    # array fields are excluded from the narrow table
    assert "tools" not in cols


def test_derive_columns_wide_includes_more():
    narrow = derive_columns(AGENT_SCHEMA, wide=False)
    wide = derive_columns(AGENT_SCHEMA, wide=True)
    assert len(wide) >= len(narrow)


def test_render_json_list():
    out = render([{"id": "a"}, {"id": "b"}], fmt="json")
    assert json.loads(out) == [{"id": "a"}, {"id": "b"}]


def test_render_yaml_single():
    out = render({"id": "a", "model": "gpt"}, fmt="yaml")
    assert yaml.safe_load(out) == {"id": "a", "model": "gpt"}


def test_render_name_prints_ids():
    out = render([{"id": "a"}, {"id": "b"}], fmt="name")
    assert out.split() == ["a", "b"]


def test_render_table_contains_values():
    out = render([{"id": "a1", "model": "gpt"}], fmt="table", columns=["id", "model"])
    assert "a1" in out
    assert "gpt" in out
    assert "ID" in out.upper()
