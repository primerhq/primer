import pytest

from primectl.filters import build_predicate, coerce_value, FilterError


def test_single_eq_filter():
    pred = build_predicate(["name=foo"])
    assert pred == {
        "kind": "predicate",
        "left": {"kind": "field", "name": "name"},
        "op": "=",
        "right": {"kind": "value", "value": "foo"},
    }


def test_op_prefix_translation():
    pred = build_predicate(["count=gt:3"])
    assert pred["op"] == ">"
    assert pred["right"]["value"] == 3


def test_two_filters_and_combined():
    pred = build_predicate(["a=1", "b=2"])
    assert pred["op"] == "and"
    assert pred["left"]["left"]["name"] == "a"
    assert pred["right"]["left"]["name"] == "b"


def test_empty_filters_returns_none():
    assert build_predicate([]) is None


def test_value_coercion():
    assert coerce_value("3") == 3
    assert coerce_value("3.5") == 3.5
    assert coerce_value("true") is True
    assert coerce_value("false") is False
    assert coerce_value("null") is None
    assert coerce_value("hello") == "hello"
    assert coerce_value("str:3") == "3"  # forced string


def test_unknown_op_raises():
    with pytest.raises(FilterError):
        build_predicate(["a=bogus:1"])


def test_malformed_filter_raises():
    with pytest.raises(FilterError):
        build_predicate(["noequals"])
