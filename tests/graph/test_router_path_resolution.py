"""Path resolution supports dotted segments + bracket indices and
returns (found, value) where found=False for any missing segment."""

from __future__ import annotations

from primer.graph.router import _resolve_path


def test_dotted_path_walks_nested_dicts() -> None:
    found, val = _resolve_path({"a": {"b": {"c": 1}}}, "a.b.c")
    assert found is True
    assert val == 1


def test_missing_key_returns_not_found() -> None:
    found, val = _resolve_path({"a": {"b": 1}}, "a.x")
    assert found is False
    assert val is None


def test_intermediate_non_dict_returns_not_found() -> None:
    found, val = _resolve_path({"a": "string"}, "a.b")
    assert found is False


def test_bracket_index_walks_into_list() -> None:
    found, val = _resolve_path({"items": [{"k": 7}, {"k": 9}]}, "items[1].k")
    assert found is True
    assert val == 9


def test_bracket_index_out_of_range_returns_not_found() -> None:
    found, val = _resolve_path({"items": ["a", "b"]}, "items[5]")
    assert found is False


def test_bracket_on_non_list_returns_not_found() -> None:
    found, val = _resolve_path({"items": {"a": 1}}, "items[0]")
    assert found is False


def test_top_level_index() -> None:
    found, val = _resolve_path([10, 20, 30], "[2]")
    assert found is True
    assert val == 30


def test_none_value_resolves_as_found() -> None:
    """A path that lands on a literal `None` is FOUND (the value is None,
    but the key was present). This matters for the missing-path-False
    rule downstream."""
    found, val = _resolve_path({"a": None}, "a")
    assert found is True
    assert val is None
