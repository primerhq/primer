"""Tests for the shared opaque-cursor encode/decode helpers."""

from __future__ import annotations

import pytest

from primer.model.common import Identifiable
from primer.model.except_ import BadRequestError
from primer.model.storage import OrderBy
from primer.storage._cursor import (
    _decode_cursor,
    _encode_cursor_for,
    _resolve_dotted,
)


class _Sample(Identifiable):
    name: str
    count: int


def test_encode_decode_roundtrip_with_no_orderby():
    entity = _Sample(id="abc", name="x", count=3)
    cursor = _encode_cursor_for(entity, order_by=None)
    decoded = _decode_cursor(cursor)
    # No order keys -> only the implicit id-ASC tiebreaker.
    assert decoded == {"keys": [{"field": "id", "value": "abc", "direction": "asc"}]}


def test_encode_decode_roundtrip_with_orderby():
    entity = _Sample(id="abc", name="x", count=3)
    cursor = _encode_cursor_for(
        entity, order_by=[OrderBy(field="count", direction="desc")]
    )
    decoded = _decode_cursor(cursor)
    assert decoded == {
        "keys": [
            {"field": "count", "value": 3, "direction": "desc"},
            {"field": "id", "value": "abc", "direction": "asc"},
        ],
    }


def test_decode_malformed_raises_bad_request():
    with pytest.raises(BadRequestError):
        _decode_cursor("!!!not-base64!!!")


def test_resolve_dotted_nested():
    assert _resolve_dotted({"a": {"b": 1}}, "a.b") == 1
    assert _resolve_dotted({"a": {"b": 1}}, "a.missing") is None
    assert _resolve_dotted({}, "a.b") is None
